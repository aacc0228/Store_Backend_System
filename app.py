import os
import hashlib
import logging
import pyodbc
import requests
import json
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from dotenv import load_dotenv

# 載入 .env 檔案中的環境變數
load_dotenv()

# --- 1. 從環境變數讀取設定 ---
DB_TYPE = os.environ.get('DB_TYPE', 'SQL_SERVER')

# --- 2. 根據設定準備連線資訊 ---
db_connection_info = {}
mysql_connector = None

if DB_TYPE == 'SQL_SERVER':
    server = os.environ.get('DB_SERVER', 'localhost')
    database = os.environ.get('DB_DATABASE', 'Menu')
    driver = os.environ.get('DB_DRIVER', '{ODBC Driver 17 for SQL Server}')
    db_user = os.environ.get('DB_UID')
    db_password = os.environ.get('DB_PWD')

    if db_user and db_password:
        db_connection_info['string'] = f'DRIVER={driver};SERVER={server};DATABASE={database};UID={db_user};PWD={db_password};'
    else: # Fallback to trusted connection for local development
        db_connection_info['string'] = f'DRIVER={driver};SERVER={server};DATABASE={database};Trusted_Connection=yes;'

elif DB_TYPE == 'MYSQL':
    import mysql.connector
    db_connection_info['config'] = {
        'host': os.environ.get('DB_HOST'),
        'user': os.environ.get('DB_USER'),
        'password': os.environ.get('DB_PASSWORD'),
        'database': os.environ.get('DB_DATABASE')
    }
else:
    raise ValueError("DB_TYPE 環境變數設定錯誤，請使用 'SQL_SERVER' 或 'MYSQL'.")


app = Flask(__name__)
app.secret_key = 'a_very_secret_and_secure_key_for_session'

logging.basicConfig(
    filename='app.log', level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s', encoding='utf-8'
)

ADMIN_PAGE = 'admin.html'

# --- Gemini API 相關設定 ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"

def translate_text_with_gemini(text, target_language_name):
    """使用 Gemini API 翻譯文字"""
    if not GEMINI_API_KEY:
        logging.error("Gemini API 金鑰未設定。")
        return None

    prompt = f"""你是一個專業的多語言翻譯專家。請將以下內容翻譯為目標語言，保持語境和語調的準確性。

## 翻譯要求：
1. **保持語境**：確保翻譯後的內容符合目標語言的文化背景
2. **專業術語**：使用正確的餐飲專業術語
3. **語調一致**：保持原文的語調和風格
4. **格式保持**：保持原有的格式和結構

要翻譯的內容：'{text}'
目標語言：{target_language_name}

請只回傳翻譯後的文字，不要包含任何其他說明或標籤。"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    headers = {'Content-Type': 'application/json'}
    
    try:
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        if result.get('candidates'):
            return result['candidates'][0]['content']['parts'][0]['text'].strip()
        else:
            logging.error(f"Gemini API 回應格式錯誤: {result}")
            return None
    except requests.exceptions.RequestException as e:
        logging.error(f"呼叫 Gemini API 時發生錯誤: {e}")
        return None

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

def check_credentials(username, password):
    password_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    sql_query = f"SELECT password FROM account WHERE username = {param_marker};"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql_query, (username,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row and password_hash == row[0]:
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

# --- 新增的 Health Check 路由 ---
@app.route('/health')
def health_check():
    """提供一個簡單的健康檢查端點"""
    db_status = "ok"
    http_status = 200
    try:
        # 嘗試連接資料庫並執行一個簡單的查詢
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"健康檢查失敗：資料庫連線錯誤: {e}")
        db_status = "error"
        http_status = 503  # Service Unavailable

    return jsonify({
        "status": "ok" if http_status == 200 else "error",
        "services": {
            "database": db_status
        }
    }), http_status

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

@app.route('/api/auto_translate', methods=['POST'])
def auto_translate():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    text_to_translate = data.get('text')
    target_langs = data.get('target_langs', [])

    if not text_to_translate or not target_langs:
        return jsonify({"error": "缺少必要參數"}), 400

    translations = {}
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
        
        for lang_code in target_langs:
            cursor.execute(f"SELECT lang_name FROM languages WHERE lang_code = {param_marker}", (lang_code,))
            result = cursor.fetchone()
            if result:
                lang_name = result[0]
                translated_text = translate_text_with_gemini(text_to_translate, lang_name)
                if translated_text:
                    translations[lang_code] = translated_text
        
        cursor.close()
        conn.close()
        return jsonify(translations)
    except Exception as ex:
        logging.error(f"自動翻譯 API 發生錯誤: {ex}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/languages', methods=['GET'])
def get_languages():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    search_term = request.args.get('search', '', type=str)
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = "SELECT lang_code, lang_name FROM languages"
        params = []
        if search_term:
            query += f" WHERE lang_code LIKE {param_marker} OR lang_name LIKE {param_marker}"
            params.extend([f"%{search_term}%", f"%{search_term}%"])
        query += " ORDER BY lang_code;"
        cursor.execute(query, params)
        languages = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return jsonify(languages)
    except Exception as ex:
        logging.error(f"API Languages 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500

@app.route('/api/languages/add', methods=['POST'])
def add_language():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    lang_code = data.get('lang_code')
    lang_name = data.get('lang_name')
    if not lang_code or not lang_name:
        return jsonify({"error": "語言代碼和名稱為必填項"}), 400
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO languages (lang_code, lang_name) VALUES ({param_marker}, {param_marker})", (lang_code, lang_name))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "message": "語言新增成功"})
    except Exception as ex:
        logging.error(f"API Add Language 資料庫錯誤: {ex}")
        if "duplicate key" in str(ex).lower() or "unique constraint" in str(ex).lower():
             return jsonify({"error": f"語言代碼 '{lang_code}' 已存在"}), 409
        return jsonify({"error": "Database error"}), 500

@app.route('/api/languages/edit', methods=['POST'])
def edit_language():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    original_lang_code = data.get('original_lang_code')
    lang_name = data.get('lang_name')
    if not original_lang_code or not lang_name:
        return jsonify({"error": "缺少必要資訊"}), 400
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE languages SET lang_name = {param_marker} WHERE lang_code = {param_marker}", (lang_name, original_lang_code))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "message": "語言更新成功"})
    except Exception as ex:
        logging.error(f"API Edit Language 資料庫錯誤: {ex}")
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
            cursor.execute(f"SELECT * FROM stores WHERE store_id = {param_marker}", (store_id,))
            store_row = cursor.fetchone()
            if store_row:
                columns = [column[0] for column in cursor.description]
                store_dict = dict(zip(columns, store_row))
                cursor.close()
                conn.close()
                return render_template('edit_store.html', store=store_dict)
            else:
                cursor.close()
                conn.close()
                return redirect(url_for('admin'))

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

@app.route('/edit_menu_item/<int:item_id>', methods=['GET', 'POST'])
def edit_menu_item(item_id):
    if 'username' not in session:
        flash('請先登入。')
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if request.method == 'POST':
        new_item_name = request.form.get('item_name')
        price_small = request.form.get('price_small')
        if not new_item_name or not price_small:
            flash('品項名稱與小份價格為必填欄位。')
            return redirect(url_for('edit_menu_item', item_id=item_id))
        
        try:
            cursor.execute(f"SELECT m.store_id FROM menu_items mi JOIN menus m ON mi.menu_id = m.menu_id WHERE mi.menu_item_id = {param_marker}", (item_id,))
            result = cursor.fetchone()
            store_id = result[0] if result else None

            price_big = request.form.get('price_big') or None
            cursor.execute(f"UPDATE menu_items SET item_name={param_marker}, price_big={param_marker}, price_small={param_marker} WHERE menu_item_id={param_marker}",
                           (new_item_name, price_big, price_small, item_id))

            cursor.execute(f"DELETE FROM menu_translations WHERE menu_item_id={param_marker}", (item_id,))
            
            lang_codes = request.form.getlist('lang_codes[]')
            descriptions = request.form.getlist('descriptions[]')
            if lang_codes and descriptions:
                for code, desc in zip(lang_codes, descriptions):
                    if code and desc:
                        cursor.execute(f"INSERT INTO menu_translations (menu_item_id, lang_code, description) VALUES ({param_marker}, {param_marker}, {param_marker})",
                                       (item_id, code, desc))

            conn.commit()
            flash(f"品項 '{new_item_name}' 更新成功！")
            return redirect(url_for('admin', tab='menu', store_id=store_id))
        except Exception as ex:
            conn.rollback()
            flash('更新品項失敗，資料庫發生錯誤。')
            logging.error(f"更新菜單品項時資料庫錯誤: {ex}")
            return redirect(url_for('edit_menu_item', item_id=item_id))
        finally:
            cursor.close()
            conn.close()

    # GET 請求
    try:
        query_item = f"""
            SELECT mi.*, s.store_id, s.store_name
            FROM menu_items mi
            JOIN menus m ON mi.menu_id = m.menu_id
            JOIN stores s ON m.store_id = s.store_id
            WHERE mi.menu_item_id = {param_marker}
        """
        cursor.execute(query_item, (item_id,))
        item_row = cursor.fetchone()
        if not item_row:
            flash('找不到該菜單品項。')
            return redirect(url_for('admin', tab='menu'))
        
        columns = [c[0] for c in cursor.description]
        item = dict(zip(columns, item_row))
        item['translations'] = {}

        cursor.execute(f"SELECT lang_code, description FROM menu_translations WHERE menu_item_id = {param_marker}", (item_id,))
        for row in cursor.fetchall():
            item['translations'][row[0]] = row[1]

        cursor.execute("SELECT lang_code, lang_name FROM languages ORDER BY lang_code;")
        columns = [c[0] for c in cursor.description]
        languages = [dict(zip(columns, row)) for row in cursor.fetchall()]

        return render_template('edit_menu_item.html', item=item, store=item, languages=languages)

    except Exception as ex:
        flash('讀取品項資料時發生錯誤。')
        logging.error(f"讀取菜單品項時錯誤: {ex}")
        return redirect(url_for('admin', tab='menu'))
    finally:
        cursor.close()
        conn.close()

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
