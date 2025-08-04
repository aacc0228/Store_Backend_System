import os
import hashlib
import logging
import pyodbc
import configparser
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

# --- 1. 讀取設定檔 ---
config = configparser.ConfigParser()
config.read('config.ini')

DB_TYPE = config.get('DATABASE', 'TYPE', fallback='SQL_SERVER')

# --- 2. 根據設定檔準備連線資訊 ---
db_connection_info = {}
mysql_connector = None
if DB_TYPE == 'SQL_SERVER':
    server = config.get('SQL_SERVER', 'SERVER')
    database = config.get('SQL_SERVER', 'DATABASE')
    driver = config.get('SQL_SERVER', 'DRIVER')
    if config.getboolean('SQL_SERVER', 'TRUSTED_CONNECTION', fallback=False):
        db_connection_info['string'] = f'DRIVER={driver};SERVER={server};DATABASE={database};Trusted_Connection=yes;'
    else:
        uid = config.get('SQL_SERVER', 'UID')
        pwd = config.get('SQL_SERVER', 'PWD')
        db_connection_info['string'] = f'DRIVER={driver};SERVER={server};DATABASE={database};UID={uid};PWD={pwd};'

elif DB_TYPE == 'MYSQL':
    import mysql.connector
    db_connection_info['config'] = {
        'host': config.get('MYSQL_CLOUD', 'HOST'),
        'user': config.get('MYSQL_CLOUD', 'USER'),
        'password': config.get('MYSQL_CLOUD', 'PASSWORD'),
        'database': config.get('MYSQL_CLOUD', 'DATABASE')
    }
else:
    raise ValueError("Unsupported database type in config.ini. Use 'SQL_SERVER' or 'MYSQL'.")


app = Flask(__name__)
app.secret_key = 'a_very_secret_and_secure_key_for_session'

logging.basicConfig(
    filename='app.log', level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s', encoding='utf-8'
)

ADMIN_PAGE = 'admin.html'

# --- 3. 建立一個通用的資料庫連線函式 ---
def get_db_connection():
    """根據設定檔建立並回傳資料庫連線"""
    try:
        if DB_TYPE == 'SQL_SERVER':
            return pyodbc.connect(db_connection_info['string'])
        elif DB_TYPE == 'MYSQL':
            return mysql.connector.connect(**db_connection_info['config'])
    except Exception as e:
        logging.error(f"資料庫連線失敗: {e}")
        raise

# --- 4. 修改所有函式以使用新的連線方式 ---
def check_credentials(username, password):
    password_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    sql_query = f"SELECT password FROM Account WHERE username = {param_marker};"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql_query, (username,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row and password_hash == row[0]: # Use index for compatibility
            return True
    except Exception as ex:
        logging.error(f"驗證時資料庫錯誤: {ex}")
    return False

# --- API Endpoints ---
@app.route('/api/stores')
def get_stores():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    page = request.args.get('page', 1, type=int)
    search_name = request.args.get('name', '', type=str)
    search_level = request.args.get('level', '', type=str)
    per_page = 10
    offset = (page - 1) * per_page
    params, where_clauses = [], []
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if search_name:
        where_clauses.append(f"store_name LIKE {param_marker}")
        params.append(f"%{search_name}%")
    if search_level:
        where_clauses.append(f"partner_level = {param_marker}")
        params.append(search_level)
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        count_query = f"SELECT COUNT(*) FROM stores {where_sql};"
        cursor.execute(count_query, params)
        total_stores = cursor.fetchone()[0]

        if DB_TYPE == 'MYSQL':
            pagination_sql = f"ORDER BY store_id DESC LIMIT {param_marker} OFFSET {param_marker};"
            final_params = params + [per_page, offset]
        else: # SQL_SERVER
            pagination_sql = f"ORDER BY store_id DESC OFFSET {param_marker} ROWS FETCH NEXT {param_marker} ROWS ONLY;"
            final_params = params + [offset, per_page]

        data_query = f"SELECT store_id, store_name, partner_level, created_at, review_summary, top_dish_1, top_dish_2, top_dish_3, top_dish_4, top_dish_5, main_photo_url, gps_lat, gps_lng, place_id FROM stores {where_sql} {pagination_sql}"
        cursor.execute(data_query, final_params)
        
        columns = [column[0] for column in cursor.description]
        stores_data = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        total_pages = (total_stores + per_page - 1) // per_page
        return jsonify({
            'stores': stores_data,
            'pagination': { 'current_page': page, 'total_pages': total_pages, 'has_prev': page > 1, 'has_next': page < total_pages }
        })
    except Exception as ex:
        logging.error(f"API Stores 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500

@app.route('/api/all_stores')
def get_all_stores():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT store_id, store_name FROM stores ORDER BY store_id;")
        columns = [c[0] for c in cursor.description]
        stores = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return jsonify(stores)
    except Exception as ex:
        logging.error(f"API All Stores 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500

@app.route('/api/menu_items/<int:store_id>')
def get_menu_items(store_id):
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    query = f"""
        SELECT mi.menu_item_id, mi.item_name, mi.price_big, mi.price_small, l.lang_name, mt.description 
        FROM menu_items mi 
        JOIN menus m ON mi.menu_id = m.menu_id 
        LEFT JOIN menu_translations mt ON mi.menu_item_id = mt.menu_item_id 
        LEFT JOIN languages l ON mt.lang_code = l.lang_code 
        WHERE m.store_id = {param_marker} 
        ORDER BY mi.menu_item_id, l.lang_code;
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query, (store_id,))
        items_dict = {}
        for row in cursor.fetchall():
            item_id = row[0]
            if item_id not in items_dict:
                items_dict[item_id] = {'menu_item_id': row[0], 'item_name': row[1], 'price_big': row[2], 'price_small': row[3], 'translations': []}
            if row[4] and row[5]:
                items_dict[item_id]['translations'].append({'lang_name': row[4], 'description': row[5]})
        cursor.close()
        conn.close()
        return jsonify(list(items_dict.values()))
    except Exception as ex:
        logging.error(f"API Menu Items 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500

# --- 完整路由列表 ---

@app.route('/add_store', methods=['GET', 'POST'])
def add_store():
    if 'username' not in session:
        flash('請先登入。')
        return redirect(url_for('home'))
    if request.method == 'POST':
        store_name = request.form.get('store_name')
        if not store_name or request.form.get('partner_level') is None:
            flash('店家名稱與合作等級為必填欄位。')
            return redirect(url_for('add_store'))
        
        param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
        sql = f"""
            INSERT INTO stores (store_name, partner_level, gps_lat, gps_lng, place_id, review_summary, 
                                top_dish_1, top_dish_2, top_dish_3, top_dish_4, top_dish_5, main_photo_url) 
            VALUES ({",".join([param_marker]*12)});
        """
        store_data = (
            store_name, request.form.get('partner_level'), request.form.get('gps_lat') or None,
            request.form.get('gps_lng') or None, request.form.get('place_id') or None,
            request.form.get('review_summary') or None, request.form.get('top_dish_1') or None,
            request.form.get('top_dish_2') or None, request.form.get('top_dish_3') or None,
            request.form.get('top_dish_4') or None, request.form.get('top_dish_5') or None,
            request.form.get('main_photo_url') or None
        )
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(sql, store_data)
            conn.commit()
            cursor.close()
            conn.close()
            flash('店家新增成功！')
            return redirect(url_for('admin'))
        except Exception as ex:
            flash('新增店家失敗，資料庫發生錯誤。')
            logging.error(f"新增店家時資料庫錯誤: {ex}")
    return render_template('add_store.html')

@app.route('/edit_store/<int:store_id>', methods=['GET', 'POST'])
def edit_store(store_id):
    if 'username' not in session:
        flash('請先登入。')
        return redirect(url_for('home'))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if request.method == 'POST':
        store_name = request.form.get('store_name')
        if not store_name:
            flash('店家名稱為必填欄位。')
            # Re-fetch data to render the form again
            cursor.execute(f"SELECT * FROM stores WHERE store_id = {param_marker}", (store_id,))
            columns = [column[0] for column in cursor.description]
            store_row = cursor.fetchone()
            if store_row:
                store_dict = dict(zip(columns, store_row))
                return render_template('edit_store.html', store=store_dict)
        
        update_data = (
            store_name, request.form.get('partner_level'), request.form.get('gps_lat') or None,
            request.form.get('gps_lng') or None, request.form.get('place_id') or None,
            request.form.get('review_summary') or None, request.form.get('top_dish_1') or None,
            request.form.get('top_dish_2') or None, request.form.get('top_dish_3') or None,
            request.form.get('top_dish_4') or None, request.form.get('top_dish_5') or None,
            request.form.get('main_photo_url') or None, store_id
        )
        sql = f"""UPDATE stores SET store_name={param_marker}, partner_level={param_marker}, gps_lat={param_marker}, 
                     gps_lng={param_marker}, place_id={param_marker}, review_summary={param_marker}, 
                     top_dish_1={param_marker}, top_dish_2={param_marker}, top_dish_3={param_marker}, 
                     top_dish_4={param_marker}, top_dish_5={param_marker}, main_photo_url={param_marker} 
                     WHERE store_id = {param_marker};"""
        try:
            cursor.execute(sql, update_data)
            conn.commit()
            flash('店家資料更新成功！')
            return redirect(url_for('admin'))
        except Exception as ex:
            flash('更新店家失敗，資料庫發生錯誤。')
            logging.error(f"更新店家時資料庫錯誤: {ex}")
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('edit_store', store_id=store_id))


    try:
        cursor.execute(f"SELECT * FROM stores WHERE store_id = {param_marker}", (store_id,))
        columns = [column[0] for column in cursor.description]
        store_row = cursor.fetchone()
        
        if store_row:
            store_dict = dict(zip(columns, store_row))
            return render_template('edit_store.html', store=store_dict)
        else:
            flash('找不到該店家資料。')
            return redirect(url_for('admin'))
    except Exception as ex:
        flash('讀取店家資料時發生錯誤。')
        logging.error(f"讀取店家資料時錯誤: {ex}")
        return redirect(url_for('admin'))
    finally:
        cursor.close()
        conn.close()

# @app.route('/add_menu_item/<int:store_id>', methods=['GET', 'POST'])
# def add_menu_item(store_id):
#     if 'username' not in session:
#         flash('請先登入。')
#         return redirect(url_for('home'))
#     # ... (The rest of the function is commented out as requested)

@app.route('/')
def home():
    if 'username' in session: return redirect(url_for('admin'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username')
    password = request.form.get('password')
    if check_credentials(username, password):
        session['username'] = username
        return redirect(url_for('admin'))
    flash('帳號或密碼錯誤！')
    return redirect(url_for('home'))

@app.route('/admin')
def admin():
    if 'username' in session: return render_template(ADMIN_PAGE, username=session['username'])
    flash('請先登入才能存取此頁面。')
    return redirect(url_for('home'))

@app.route('/logout')
def logout():
    session.pop('username', None)
    flash('您已成功登出。')
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=True)
