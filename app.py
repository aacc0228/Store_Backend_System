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

    # 使用一個更簡潔、直接的 Prompt，以獲得更穩定的結果
    prompt = f"請將這個菜單品項 '{text}' 翻譯成專業且道地的'{target_language_name}'。請只回傳翻譯後的文字，不要加上任何引號、標籤或說明。"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    headers = {'Content-Type': 'application/json'}
    
    try:
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        # 增加詳細的日誌記錄，以便除錯
        logging.info(f"Gemini API Raw Response: {json.dumps(result, ensure_ascii=False)}")
        
        if result.get('candidates'):
            translated_text = result['candidates'][0]['content']['parts'][0]['text'].strip()
            logging.info(f"Translated '{text}' to '{target_language_name}': '{translated_text}'")
            return translated_text
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
    # 將 account 改為 Account 以增加相容性
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

# --- 驗證函式 ---
def validate_store_data(form):
    """檢查店家資料是否超過欄位長度限制"""
    VALIDATION_LIMITS = {
        'store_name': 100, 'place_id': 255, 'top_dish_1': 100,
        'top_dish_2': 100, 'top_dish_3': 100, 'top_dish_4': 100,
        'top_dish_5': 100, 'main_photo_url': 255,
    }
    FIELD_NAMES = {
        'store_name': '店家名稱', 'place_id': 'Google Map Place ID',
        'top_dish_1': '人氣菜色 1', 'top_dish_2': '人氣菜色 2',
        'top_dish_3': '人氣菜色 3', 'top_dish_4': '人氣菜色 4',
        'top_dish_5': '人氣菜色 5', 'main_photo_url': '店家招牌照片 URL',
    }
    for field, limit in VALIDATION_LIMITS.items():
        value = form.get(field)
        if value and len(value) > limit:
            return f"'{FIELD_NAMES.get(field, field)}' 的長度不可超過 {limit} 個字元。"
    return None

def validate_menu_item_data(form):
    """檢查菜單品項資料是否超過欄位長度限制"""
    if len(form.get('item_name', '')) > 100:
        return "'品項名稱' 的長度不可超過 100 個字元。"
    return None

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
        LEFT JOIN languages l ON mt.lang_code = l.translation_lang_code
        WHERE m.store_id = {param_marker} 
        ORDER BY mi.menu_item_id, l.line_lang_code;
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

@app.route('/api/languages', methods=['GET'])
def get_languages():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    search_term = request.args.get('search', '', type=str)
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = "SELECT line_lang_code, lang_name, translation_lang_code, stt_lang_code FROM languages"
        params = []
        if search_term:
            query += f" WHERE line_lang_code LIKE {param_marker} OR lang_name LIKE {param_marker}"
            params.extend([f"%{search_term}%", f"%{search_term}%"])
        query += " ORDER BY line_lang_code;"
        cursor.execute(query, params)
        languages = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return jsonify(languages)
    except Exception as ex:
        logging.error(f"API Languages 資料庫錯誤: {ex}")
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
            cursor.execute(f"SELECT lang_name FROM languages WHERE translation_lang_code = {param_marker}", (lang_code,))
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

@app.route('/api/languages/add', methods=['POST'])
def add_language():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    line_lang_code = data.get('line_lang_code')
    lang_name = data.get('lang_name')
    translation_lang_code = data.get('translation_lang_code')
    stt_lang_code = data.get('stt_lang_code')

    if not all([line_lang_code, lang_name, translation_lang_code, stt_lang_code]):
        return jsonify({"error": "所有欄位皆為必填項"}), 400
    
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO languages (line_lang_code, lang_name, translation_lang_code, stt_lang_code) VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})", 
                       (line_lang_code, lang_name, translation_lang_code, stt_lang_code))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "message": "語言新增成功"})
    except Exception as ex:
        logging.error(f"API Add Language 資料庫錯誤: {ex}")
        if "duplicate key" in str(ex).lower() or "unique constraint" in str(ex).lower() or "primary key" in str(ex).lower():
             return jsonify({"error": f"Line 語言代碼 '{line_lang_code}' 已存在"}), 409
        return jsonify({"error": "Database error"}), 500

@app.route('/api/languages/edit', methods=['POST'])
def edit_language():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    data = request.json
    line_lang_code = data.get('line_lang_code')
    lang_name = data.get('lang_name')
    translation_lang_code = data.get('translation_lang_code')
    stt_lang_code = data.get('stt_lang_code')

    if not all([line_lang_code, lang_name, translation_lang_code, stt_lang_code]):
        return jsonify({"error": "所有欄位皆為必填項"}), 400

    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE languages SET lang_name = {param_marker}, translation_lang_code = {param_marker}, stt_lang_code = {param_marker} WHERE line_lang_code = {param_marker}", 
                       (lang_name, translation_lang_code, stt_lang_code, line_lang_code))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"success": True, "message": "語言更新成功"})
    except Exception as ex:
        logging.error(f"API Edit Language 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500

# --- 完整路由列表 ---
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

@app.route('/add_store', methods=['GET', 'POST'])
def add_store():
    if 'username' not in session:
        flash('請先登入。')
        return redirect(url_for('home'))
    if request.method == 'POST':
        store_name = request.form.get('store_name')
        if not store_name or request.form.get('partner_level') is None:
            flash('店家名稱與合作等級為必填欄位。')
            return render_template('add_store.html', form_data=request.form)
        
        validation_error = validate_store_data(request.form)
        if validation_error:
            flash(validation_error)
            return render_template('add_store.html', form_data=request.form)
            
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
            return render_template('add_store.html', form_data=request.form)

    return render_template('add_store.html', form_data={})

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
            return render_template('edit_store.html', store=request.form)
        
        validation_error = validate_store_data(request.form)
        if validation_error:
            flash(validation_error)
            store_data_from_form = request.form.to_dict()
            store_data_from_form['store_id'] = store_id
            return render_template('edit_store.html', store=store_data_from_form)

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
        
        validation_error = validate_menu_item_data(request.form)
        if validation_error:
            flash(validation_error)
            try:
                cursor.execute(f"SELECT s.store_id, s.store_name FROM menu_items mi JOIN menus m ON mi.menu_id = m.menu_id JOIN stores s ON m.store_id = s.store_id WHERE mi.menu_item_id = {param_marker}", (item_id,))
                store_row = cursor.fetchone()
                store_info = dict(zip([c[0] for c in cursor.description], store_row)) if store_row else {}

                cursor.execute("SELECT line_lang_code, lang_name, translation_lang_code FROM languages ORDER BY line_lang_code;")
                languages = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]

                item_from_form = {
                    'menu_item_id': item_id, 'item_name': new_item_name,
                    'price_small': price_small, 'price_big': request.form.get('price_big'),
                    'translations': {}
                }
                lang_codes = request.form.getlist('lang_codes[]')
                descriptions = request.form.getlist('descriptions[]')
                if lang_codes and descriptions:
                    for code, desc in zip(lang_codes, descriptions):
                        item_from_form['translations'][code] = desc
                
                return render_template('edit_menu_item.html', item=item_from_form, store=store_info, languages=languages)
            except Exception as ex:
                 logging.error(f"重新渲染 edit_menu_item 頁面時出錯: {ex}")
                 return redirect(url_for('admin', tab='menu'))
            finally:
                cursor.close()
                conn.close()

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

        cursor.execute("SELECT line_lang_code, lang_name, translation_lang_code FROM languages ORDER BY line_lang_code;")
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

if __name__ == '__main__':
    app.run(debug=True)
