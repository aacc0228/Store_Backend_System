import os
import hashlib
import logging
import pyodbc
import requests
import json
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from dotenv import load_dotenv
from datetime import datetime
import base64

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
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"

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

def process_menu_image_with_gemini(image_bytes):
    """
    使用 Gemini Pro Vision API 辨識菜單圖片、翻譯並回傳結構化 JSON。
    """
    if not GEMINI_API_KEY:
        logging.error("Gemini API 金鑰未設定。")
        return None, "Gemini API 金鑰未設定"

    # 1. 將圖片轉換為 Base64 編碼
    base64_image = base64.b64encode(image_bytes).decode('utf-8')

    # 2. 構造強大的 Prompt
    prompt = """
    你是一位專業的菜單資料分析師。請分析這張菜單圖片，並遵循以下指示：
    1. 辨識出所有的菜單品項及其價格。如果一個品項有大小份的價格，請分別標示。
    2. 將每個品項的名稱翻譯成專業且道地的英文。
    3. 忽略任何非品項的裝飾性文字或描述。
    4. 將結果格式化為一個 JSON 物件，頂層需有一個名為 "menu_items" 的 key，其 value 是一個包含所有品項的 array。
    5. 每個品項物件應包含以下 key：
       - "original_name": 原始的中文品項名稱 (string)。
       - "translated_name": 翻譯後的英文品項名稱 (string)。
       - "price_small": 小份或單一價格 (number)。
       - "price_large": 大份的價格 (number)，如果沒有則為 null。
    
    範例輸出:
    {
      "menu_items": [
        {
          "original_name": "珍珠奶茶",
          "translated_name": "Pearl Milk Tea",
          "price_small": 50,
          "price_large": 65
        },
        {
          "original_name": "牛肉麵",
          "translated_name": "Beef Noodle Soup",
          "price_small": 150,
          "price_large": null
        }
      ]
    }
    如果圖片無法辨識或不是菜單，請回傳 {"menu_items": []}。
    請直接回傳 JSON 內容，不要包含任何額外的說明或 markdown 標記 (例如 ```json)。
    """

    # 3. 構造 API Payload
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": base64_image
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
        }
    }
    
    headers = {'Content-Type': 'application/json'}
    
    try:
        # *** 修改處 START ***
        # 確保 vision_api_url 是一個乾淨的 F-string 字串
        vision_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}"
        
        # 新增日誌，印出最終要呼叫的 URL，以供驗證
        logging.info(f"準備呼叫 Gemini Vision API, URL: {vision_api_url}")
        # *** 修改處 END ***

        response = requests.post(vision_api_url, headers=headers, json=payload, timeout=90)
        
        logging.info(f"Gemini Vision API Raw Response Text: {response.text}")
        response.raise_for_status()
        result = response.json()
        
        if result.get('candidates'):
            response_text = result['candidates'][0]['content']['parts'][0]['text']
            parsed_json = json.loads(response_text)
            return parsed_json, None
        else:
            error_details = json.dumps(result, ensure_ascii=False)
            logging.error(f"Gemini Vision API 回應格式錯誤: {error_details}")
            return None, f"API 回應格式錯誤: {error_details}"

    except requests.exceptions.RequestException as e:
        logging.error(f"呼叫 Gemini Vision API 時發生錯誤: {e}")
        return None, f"呼叫 API 時發生錯誤: {e}"
    except json.JSONDecodeError as e:
        logging.error(f"解析 Gemini Vision API 回應的 JSON 時失敗: {e}")
        return None, "解析 API 回應時失敗"
    except Exception as e:
        logging.error(f"處理 Gemini Vision API 請求時發生未知錯誤: {e}")
        return None, "發生未知錯誤"

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

def validate_ocr_menu_item_data(form):
    """檢查 OCR 菜單品項資料是否超過欄位長度限制"""
    if len(form.get('item_name', '')) > 100:
        return "'品項名稱' 的長度不可超過 100 個字元。"
    # 可在此處為 translated_desc 新增長度檢查
    # if len(form.get('translated_desc', '')) > 500:
    #     return "'翻譯後介紹' 的長度不可超過 500 個字元。"
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

@app.route('/api/ocr_store_names')
def get_ocr_store_names():
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT store_name FROM ocr_menus WHERE store_name IS NOT NULL ORDER BY store_name;")
        store_names = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return jsonify(store_names)
    except Exception as ex:
        logging.error(f"API OCR Store Names 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500

@app.route('/api/ocr_menus/<store_name>')
def get_ocr_menu_items(store_name):
    if 'username' not in session: return jsonify({"error": "Unauthorized"}), 401
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    query = f"""
        SELECT 
            omi.ocr_menu_item_id, omi.item_name, omi.price_big, omi.price_small, omi.translated_desc,
            l.lang_name, omt.description
        FROM ocr_menu_items omi
        JOIN ocr_menus om ON omi.ocr_menu_id = om.ocr_menu_id
        LEFT JOIN ocr_menu_translations omt ON omi.ocr_menu_item_id = omt.menu_item_id
        LEFT JOIN languages l ON omt.lang_code = l.translation_lang_code
        WHERE om.store_name = {param_marker}
        ORDER BY omi.ocr_menu_item_id, l.line_lang_code;
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query, (store_name,))
        items_dict = {}
        for row in cursor.fetchall():
            item_id = row[0]
            if item_id not in items_dict:
                items_dict[item_id] = {'ocr_menu_item_id': row[0], 'item_name': row[1], 'price_big': row[2], 'price_small': row[3], 'translated_desc': row[4], 'translations': []}
            if row[5] and row[6]:
                items_dict[item_id]['translations'].append({'lang_name': row[5], 'description': row[6]})
        cursor.close()
        conn.close()
        return jsonify(list(items_dict.values()))
    except Exception as ex:
        logging.error(f"API OCR Menu Items 資料庫錯誤: {ex}")
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

@app.route('/edit_ocr_menu_item/<int:item_id>', methods=['GET', 'POST'])
def edit_ocr_menu_item(item_id):
    if 'username' not in session:
        flash('請先登入。')
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if request.method == 'POST':
        item_name = request.form.get('item_name')
        price_small = request.form.get('price_small')
        
        # 取得原始店家名稱，以便在出錯或成功時能正確導向
        cursor.execute(f"""
            SELECT om.store_name 
            FROM ocr_menu_items omi 
            JOIN ocr_menus om ON omi.ocr_menu_id = om.ocr_menu_id 
            WHERE omi.ocr_menu_item_id = {param_marker}
        """, (item_id,))
        store_result = cursor.fetchone()
        store_name = store_result[0] if store_result else None

        if not item_name or not price_small:
            flash('品項名稱與小份價格為必填欄位。')
            return redirect(url_for('edit_ocr_menu_item', item_id=item_id))
        
        validation_error = validate_ocr_menu_item_data(request.form)
        if validation_error:
            flash(validation_error)
            # 如果驗證失敗，需要重新準備資料以渲染範本
            # 這裡我們直接導向回 GET 請求，簡化處理
            return redirect(url_for('edit_ocr_menu_item', item_id=item_id))

        try:
            # 1. 更新 ocr_menu_items 主表
            price_big = request.form.get('price_big') or None
            translated_desc = request.form.get('translated_desc') or None
            cursor.execute(f"""
                UPDATE ocr_menu_items 
                SET item_name={param_marker}, price_big={param_marker}, price_small={param_marker}, translated_desc={param_marker}
                WHERE ocr_menu_item_id={param_marker}
            """, (item_name, price_big, price_small, translated_desc, item_id))

            # 2. 刪除舊的多語言翻譯
            cursor.execute(f"DELETE FROM ocr_menu_translations WHERE menu_item_id={param_marker}", (item_id,))
            
            # 3. 插入新的多語言翻譯
            lang_codes = request.form.getlist('lang_codes[]')
            descriptions = request.form.getlist('descriptions[]')
            if lang_codes and descriptions:
                for code, desc in zip(lang_codes, descriptions):
                    if code and desc: # 確保語言代碼和描述都有值
                        cursor.execute(f"""
                            INSERT INTO ocr_menu_translations (menu_item_id, lang_code, description) 
                            VALUES ({param_marker}, {param_marker}, {param_marker})
                        """, (item_id, code, desc))

            conn.commit()
            flash(f"OCR 品項 '{item_name}' 更新成功！")
            
            # 導向回 OCR 管理頁面，並選定剛才的店家
            return redirect(url_for('admin', tab='ocr', store_name=store_name))

        except Exception as ex:
            conn.rollback()
            flash('更新 OCR 品項失敗，資料庫發生錯誤。')
            logging.error(f"更新 OCR 菜單品項時資料庫錯誤: {ex}")
            return redirect(url_for('edit_ocr_menu_item', item_id=item_id))
        finally:
            cursor.close()
            conn.close()

    # 處理 GET 請求
    try:
        # 查詢品項本身以及其所屬的店家名稱
        query_item = f"""
            SELECT omi.*, om.store_name
            FROM ocr_menu_items omi
            JOIN ocr_menus om ON omi.ocr_menu_id = om.ocr_menu_id
            WHERE omi.ocr_menu_item_id = {param_marker}
        """
        cursor.execute(query_item, (item_id,))
        item_row = cursor.fetchone()

        if not item_row:
            flash('找不到該 OCR 菜單品項。')
            return redirect(url_for('admin', tab='ocr'))
        
        columns = [c[0] for c in cursor.description]
        item_data = dict(zip(columns, item_row))
        # 建立一個 store 的物件，讓範本可以一致地存取 store.store_name
        store_data = {'store_name': item_data['store_name']}

        # 查詢品項的多語言翻譯
        item_data['translations'] = {}
        cursor.execute(f"SELECT lang_code, description FROM ocr_menu_translations WHERE menu_item_id = {param_marker}", (item_id,))
        for row in cursor.fetchall():
            item_data['translations'][row[0]] = row[1]

        # 查詢所有可用的語言以填充下拉選單
        cursor.execute("SELECT line_lang_code, lang_name, translation_lang_code FROM languages ORDER BY line_lang_code;")
        lang_columns = [c[0] for c in cursor.description]
        languages = [dict(zip(lang_columns, row)) for row in cursor.fetchall()]

        return render_template('edit_ocr_menu_item.html', item=item_data, store=store_data, languages=languages)

    except Exception as ex:
        flash('讀取 OCR 品項資料時發生錯誤。')
        logging.error(f"讀取 OCR 菜單品項時錯誤: {ex}")
        return redirect(url_for('admin', tab='ocr'))
    finally:
        cursor.close()
        conn.close()

# app.py

# ... (檔案的其他部分保持不變) ...

@app.route('/import_ocr_menu', methods=['POST'])
def import_ocr_menu():
    """
    將指定 OCR 店家名稱的所有菜單項目匯入到正式的菜單系統中，
    並在成功後刪除原始的 OCR 資料。
    """
    if 'username' not in session:
        flash('請先登入。', 'error')
        return redirect(url_for('home'))

    ocr_store_name = request.form.get('ocr_store_name')
    if not ocr_store_name:
        flash('未提供店家名稱，無法匯入。', 'error')
        return redirect(url_for('admin', tab='ocr'))

    conn = get_db_connection()
    if DB_TYPE == 'MYSQL':
        conn.autocommit = False
    
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    try:
        # 步驟 1: 驗證店家是否存在於 `stores` 表，並取得 `store_id`
        cursor.execute(f"SELECT store_id FROM stores WHERE store_name = {param_marker}", (ocr_store_name,))
        store_row = cursor.fetchone()
        if not store_row:
            flash(f"匯入失敗：在正式店家列表中找不到名為 '{ocr_store_name}' 的店家。請先新增店家資料。", 'error')
            conn.close()
            return redirect(url_for('admin', tab='ocr'))
        store_id = store_row[0]

        # 步驟 2: 建立菜單 ID (menu_id)
        current_time = datetime.now()
        sql_insert_menu = f"""
            INSERT INTO menus (store_id, version, effective_date, created_at) 
            VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})
        """
        cursor.execute(sql_insert_menu, (store_id, 1, current_time, current_time))
        if DB_TYPE == 'MYSQL':
            menu_id = cursor.lastrowid
        else: # SQL_SERVER
            cursor.execute("SELECT @@IDENTITY AS id")
            menu_id = cursor.fetchone()[0]
        
        # 步驟 3: 取得此 OCR 店家的所有菜單項目
        columns_item = ['ocr_menu_item_id', 'item_name', 'price_big', 'price_small']
        query_ocr_items = f"""
            SELECT omi.ocr_menu_item_id, omi.item_name, omi.price_big, omi.price_small
            FROM ocr_menu_items omi
            JOIN ocr_menus om ON omi.ocr_menu_id = om.ocr_menu_id
            WHERE om.store_name = {param_marker}
        """
        cursor.execute(query_ocr_items, (ocr_store_name,))
        ocr_items = [dict(zip(columns_item, row)) for row in cursor.fetchall()]

        if not ocr_items:
            flash(f"店家 '{ocr_store_name}' 沒有可匯入的 OCR 菜單項目。", 'success')
            conn.close()
            return redirect(url_for('admin', tab='ocr'))

        # 步驟 4-6: 遍歷、插入品項和翻譯
        imported_count = 0
        for ocr_item in ocr_items:
            cursor.execute(
                f"INSERT INTO menu_items (menu_id, item_name, price_big, price_small) VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})",
                (menu_id, ocr_item['item_name'], ocr_item.get('price_big'), ocr_item.get('price_small'))
            )
            if DB_TYPE == 'MYSQL':
                new_menu_item_id = cursor.lastrowid
            else: # SQL_SERVER
                cursor.execute("SELECT @@IDENTITY AS id")
                new_menu_item_id = cursor.fetchone()[0]

            columns_trans = ['lang_code', 'description']
            cursor.execute(
                f"SELECT lang_code, description FROM ocr_menu_translations WHERE menu_item_id = {param_marker}",
                (ocr_item['ocr_menu_item_id'],)
            )
            ocr_translations = [dict(zip(columns_trans, row)) for row in cursor.fetchall()]
            
            for trans in ocr_translations:
                cursor.execute(
                    f"INSERT INTO menu_translations (menu_item_id, lang_code, description) VALUES ({param_marker}, {param_marker}, {param_marker})",
                    (new_menu_item_id, trans['lang_code'], trans['description'])
                )
            imported_count += 1
        
        # --- *** 新增的刪除邏輯 START *** ---
        # 步驟 7: 匯入成功後，刪除原始 OCR 資料
        # 為了避免外鍵約束問題，刪除順序為：translations -> items -> menus
        logging.info(f"開始為店家 '{ocr_store_name}' 刪除已匯入的 OCR 資料...")

        # 7.1 刪除 ocr_menu_translations
        # 使用子查詢，刪除所有與該店家相關的翻譯
        delete_translations_sql = f"""
            DELETE FROM ocr_menu_translations 
            WHERE menu_item_id IN (
                SELECT omi.ocr_menu_item_id FROM ocr_menu_items omi
                JOIN ocr_menus om ON omi.ocr_menu_id = om.ocr_menu_id
                WHERE om.store_name = {param_marker}
            )
        """
        cursor.execute(delete_translations_sql, (ocr_store_name,))
        logging.info(f"刪除了 {cursor.rowcount} 筆 OCR 翻譯。")

        # 7.2 刪除 ocr_menu_items
        # 使用子查詢，刪除所有與該店家相關的品項
        delete_items_sql = f"""
            DELETE FROM ocr_menu_items 
            WHERE ocr_menu_id IN (
                SELECT ocr_menu_id FROM ocr_menus WHERE store_name = {param_marker}
            )
        """
        cursor.execute(delete_items_sql, (ocr_store_name,))
        logging.info(f"刪除了 {cursor.rowcount} 筆 OCR 品項。")

        # 7.3 刪除 ocr_menus
        cursor.execute(f"DELETE FROM ocr_menus WHERE store_name = {param_marker}", (ocr_store_name,))
        logging.info(f"刪除了 {cursor.rowcount} 筆 OCR 菜單主紀錄。")
        # --- *** 新增的刪除邏輯 END *** ---

        # 步驟 8: 提交事務 (同時保存匯入的新資料和刪除的舊資料)
        conn.commit()
        flash(f"成功為店家 '{ocr_store_name}' 匯入 {imported_count} 個菜單品項，並已清除原始 OCR 資料！", 'success')
        return redirect(url_for('admin', tab='menu', store_id=store_id))

    except Exception as e:
        # 如果任何步驟出錯，則回滾所有變更
        conn.rollback()
        logging.error(f"OCR menu import failed for store '{ocr_store_name}': {e}")
        flash(f"匯入失敗，發生嚴重錯誤：{e}", 'error')
        return redirect(url_for('admin', tab='ocr'))
    finally:
        # 確保連線被關閉
        if conn:
            conn.close()

# app.py

# ... (檔案的其他部分保持不變) ...

@app.route('/upload_ocr', methods=['GET', 'POST'])
def upload_ocr():
    """
    處理菜單圖片上傳，使用 Gemini Vision 進行辨識與翻譯，並將結果存入資料庫。
    """
    if 'username' not in session:
        flash('請先登入。', 'error')
        return redirect(url_for('home'))

    if request.method == 'POST':
        store_id = request.form.get('store_id')
        image_file = request.files.get('image')

        if not store_id or not image_file or image_file.filename == '':
            flash('店家和圖片檔案皆為必填選項。', 'error')
            return redirect(url_for('upload_ocr'))
        
        image_bytes = image_file.read()
        
        # 1. 呼叫 Gemini Vision API 處理圖片
        ocr_result, error = process_menu_image_with_gemini(image_bytes)

        if error or not ocr_result or not ocr_result.get("menu_items"):
            flash(f"菜單辨識失敗：{error or 'Gemini 未能辨識出任何菜單項目。'}", 'error')
            return redirect(url_for('upload_ocr'))

        conn = get_db_connection()
        if DB_TYPE == 'MYSQL':
            conn.autocommit = False
        cursor = conn.cursor()
        param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

        try:
            # 2. 查詢店家名稱
            cursor.execute(f"SELECT store_name FROM stores WHERE store_id = {param_marker}", (store_id,))
            store_row = cursor.fetchone()
            if not store_row:
                flash(f"找不到 Store ID 為 {store_id} 的店家。", 'error')
                return redirect(url_for('upload_ocr'))
            store_name = store_row[0]

           # 3. 寫入 ocr_menus 表
            current_time = datetime.now()  # 取得目前時間
            fixed_user_id = 99999          # 設定固定的 user_id

            sql_insert_ocr_menu = f"""
                INSERT INTO ocr_menus (store_name, store_id, user_id, upload_time) 
                VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})
            """
            cursor.execute(sql_insert_ocr_menu, (store_name, store_id, fixed_user_id, current_time))
            
            if DB_TYPE == 'MYSQL':
                ocr_menu_id = cursor.lastrowid
            else: # SQL_SERVER
                cursor.execute("SELECT @@IDENTITY AS id")
                ocr_menu_id = cursor.fetchone()[0]

            # 4. 遍歷辨識結果，寫入 ocr_menu_items 和 ocr_menu_translations
            item_count = 0
            for item in ocr_result["menu_items"]:
                original_name = item.get("original_name")
                translated_name = item.get("translated_name")
                price_small = item.get("price_small")
                price_large = item.get("price_large")

                if not original_name or price_small is None:
                    logging.warning(f"跳過不完整的項目: {item}")
                    continue

                # 4.1 寫入 ocr_menu_items
                cursor.execute(
                    f"INSERT INTO ocr_menu_items (ocr_menu_id, item_name, price_small, price_big) VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})",
                    (ocr_menu_id, original_name, price_small, price_large)
                )
                if DB_TYPE == 'MYSQL':
                    ocr_item_id = cursor.lastrowid
                else: # SQL_SERVER
                    cursor.execute("SELECT @@IDENTITY AS id")
                    ocr_item_id = cursor.fetchone()[0]

                # 4.2 寫入 ocr_menu_translations (中文)
                cursor.execute(
                    f"INSERT INTO ocr_menu_translations (menu_item_id, lang_code, description) VALUES ({param_marker}, {param_marker}, {param_marker})",
                    (ocr_item_id, 'zh-TW', original_name) # 假設中文的 lang_code 是 'zh-TW'
                )
                
                # 4.3 寫入 ocr_menu_translations (英文)
                if translated_name:
                    cursor.execute(
                        f"INSERT INTO ocr_menu_translations (menu_item_id, lang_code, description) VALUES ({param_marker}, {param_marker}, {param_marker})",
                        (ocr_item_id, 'en', translated_name) # 假設英文的 lang_code 是 'en'
                    )
                item_count += 1
            
            # 5. 提交事務
            conn.commit()
            flash(f"菜單辨識成功！已為店家 '{store_name}' 新增 {item_count} 個項目。", 'success')
            return redirect(url_for('admin', tab='ocr', store_name=store_name))

        except Exception as e:
            conn.rollback()
            logging.error(f"將 OCR 結果存入資料庫時發生錯誤: {e}")
            flash(f"辨識結果存檔失敗，發生內部錯誤: {e}", 'error')
            return redirect(url_for('upload_ocr'))
        finally:
            if conn:
                conn.close()

    # GET 請求的處理邏輯 (保持不變)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT store_id, store_name FROM stores ORDER BY store_id desc;")
        columns = [c[0] for c in cursor.description]
        stores = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return render_template('upload_ocr.html', stores=stores)
    except Exception as ex:
        logging.error(f"讀取店家列表以供上傳頁面使用時發生錯誤: {ex}")
        flash('無法讀取店家列表，請稍後再試。', 'error')
        return redirect(url_for('admin', tab='ocr'))

@app.route('/add_store_user_link', methods=['GET', 'POST'])
def add_store_user_link():
    """處理新增/修改人店綁定的獨立頁面"""
    if 'username' not in session:
        flash('請先登入。', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if request.method == 'POST':
        store_id = request.form.get('store_id')
        user_id = request.form.get('user_id')

        if not store_id or not user_id:
            flash('店家和使用者皆為必填項。', 'error')
            # POST 失敗時也需要重新載入資料以渲染範本
            return redirect(url_for('add_store_user_link'))

        sql = f"INSERT INTO store_user_link (store_id, user_id) VALUES ({param_marker}, {param_marker});"
        try:
            cursor.execute(sql, (store_id, user_id))
            conn.commit()
            flash('綁定成功！', 'success')
            return redirect(url_for('admin', tab='binding'))
        except Exception as ex:
            conn.rollback()
            if 'UNIQUE KEY constraint' in str(ex) or 'UQ_store_user_link_unique_pair' in str(ex) or 'Duplicate entry' in str(ex):
                flash('新增失敗：此綁定關係已存在。', 'error')
            else:
                flash('新增失敗，資料庫發生錯誤。', 'error')
                logging.error(f"新增人店綁定時資料庫錯誤: {ex}")
            return redirect(url_for('add_store_user_link'))
        finally:
            cursor.close()
            conn.close()

    # 處理 GET 請求
    try:
        cursor.execute("SELECT store_id, store_name FROM stores ORDER BY store_id desc;")
        stores = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        
        cursor.execute("SELECT user_id, user_name FROM users ORDER BY user_id desc;")
        users = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        
        return render_template('add_store_user_link.html', stores=stores, users=users)
    except Exception as ex:
        logging.error(f"載入新增綁定頁面時發生錯誤: {ex}")
        flash('無法載入頁面資料，請稍後再試。', 'error')
        return redirect(url_for('admin'))
    finally:
        cursor.close()
        conn.close()

@app.route('/api/all_users')
def get_all_users():
    """獲取所有使用者列表 API"""
    if 'username' not in session: 
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, user_name, line_user_id FROM users ORDER BY user_name;")
        users = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        return jsonify(users)
    except Exception as ex:
        logging.error(f"API All Users 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500
    finally:
        conn.close()

@app.route('/api/store_user_links', methods=['GET'])
def get_store_user_links():
    """獲取所有人店綁定關係 API"""
    if 'username' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    query = """
        SELECT sul.link_id, s.store_name, u.user_name
        FROM store_user_link sul
        JOIN stores s ON sul.store_id = s.store_id
        JOIN users u ON sul.user_id = u.user_id
        ORDER BY s.store_name, u.user_name;
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query)
        links = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        return jsonify(links)
    except Exception as ex:
        logging.error(f"API Get Store User Links 資料庫錯誤: {ex}")
        return jsonify({"error": "Database error"}), 500
    finally:
        conn.close()

@app.route('/api/store_user_links/delete', methods=['POST'])
def delete_store_user_link():
    """刪除人店綁定關係 API"""
    if 'username' not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    data = request.json
    link_id = data.get('link_id')

    if not link_id:
        return jsonify({"error": "缺少 link_id"}), 400

    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'
    sql = f"DELETE FROM store_user_link WHERE link_id = {param_marker};"

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql, (link_id,))
        conn.commit()
        
        if cursor.rowcount > 0:
            return jsonify({"success": True, "message": "刪除成功！"})
        else:
            return jsonify({"error": "找不到該綁定關係"}), 404
            
    except Exception as ex:
        logging.error(f"API Delete Store User Link 資料庫錯誤: {ex}")
        return jsonify({"error": "資料庫錯誤"}), 500
    finally:
        conn.close()

@app.route('/add_menu_item/<int:store_id>', methods=['GET', 'POST'])
def add_menu_item(store_id):
    if 'username' not in session:
        flash('請先登入。')
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if request.method == 'POST':
        item_name = request.form.get('item_name')
        price_small = request.form.get('price_small')

        if not item_name or not price_small:
            flash('品項名稱與小份價格為必填欄位。')
            return redirect(url_for('add_menu_item', store_id=store_id))

        validation_error = validate_menu_item_data(request.form)
        if validation_error:
            flash(validation_error)
            return redirect(url_for('add_menu_item', store_id=store_id))
        
        try:
            # 步驟 1: 查詢店家現有的最新菜單，如果沒有則建立一個
            if DB_TYPE == 'MYSQL':
                # MySQL 使用 LIMIT 1
                query = f"SELECT menu_id FROM menus WHERE store_id = {param_marker} ORDER BY version DESC LIMIT 1"
            else: # SQL_SERVER
                # SQL Server 使用 TOP 1
                query = f"SELECT TOP 1 menu_id FROM menus WHERE store_id = {param_marker} ORDER BY version DESC"
            
            cursor.execute(query, (store_id,)) # 執行修正後的查詢
            menu_row = cursor.fetchone()
            menu_id = None
            if menu_row:
                menu_id = menu_row[0]
            else:
                # 如果店家沒有任何菜單，則建立第一版
                current_time = datetime.now()
                cursor.execute(f"INSERT INTO menus (store_id, version, effective_date, created_at) VALUES ({param_marker}, 1, {param_marker}, {param_marker})",
                               (store_id, current_time, current_time))
                if DB_TYPE == 'MYSQL':
                    menu_id = cursor.lastrowid
                else: # SQL_SERVER
                    cursor.execute("SELECT @@IDENTITY AS id")
                    menu_id = cursor.fetchone()[0]

            # 步驟 2: 插入新的菜單品項
            price_big = request.form.get('price_big') or None
            cursor.execute(f"INSERT INTO menu_items (menu_id, item_name, price_big, price_small) VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})",
                           (menu_id, item_name, price_big, price_small))
            
            if DB_TYPE == 'MYSQL':
                new_item_id = cursor.lastrowid
            else: # SQL_SERVER
                cursor.execute("SELECT @@IDENTITY AS id")
                new_item_id = cursor.fetchone()[0]

            # 步驟 3: 插入對應的多語言翻譯
            lang_codes = request.form.getlist('lang_codes[]')
            descriptions = request.form.getlist('descriptions[]')
            if lang_codes and descriptions:
                for code, desc in zip(lang_codes, descriptions):
                    if code and desc:
                        cursor.execute(f"INSERT INTO menu_translations (menu_item_id, lang_code, description) VALUES ({param_marker}, {param_marker}, {param_marker})",
                                       (new_item_id, code, desc))

            conn.commit()
            flash(f"品項 '{item_name}' 新增成功！", 'success')
            return redirect(url_for('admin', tab='menu', store_id=store_id))
        except Exception as ex:
            conn.rollback()
            flash('新增品項失敗，資料庫發生錯誤。', 'error')
            logging.error(f"新增菜單品項時資料庫錯誤: {ex}")
            return redirect(url_for('add_menu_item', store_id=store_id))
        finally:
            cursor.close()
            conn.close()

    # 處理 GET 請求
    try:
        # 取得店家資訊
        cursor.execute(f"SELECT store_id, store_name FROM stores WHERE store_id = {param_marker}", (store_id,))
        store_row = cursor.fetchone()
        if not store_row:
            flash('找不到指定的店家。')
            return redirect(url_for('admin', tab='menu'))
        
        store = dict(zip([c[0] for c in cursor.description], store_row))

        # 取得所有可用語言
        cursor.execute("SELECT line_lang_code, lang_name, translation_lang_code FROM languages ORDER BY line_lang_code;")
        languages = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]

        return render_template('add_menu_item.html', store=store, languages=languages)
    except Exception as ex:
        flash('讀取店家資料時發生錯誤。')
        logging.error(f"讀取新增菜單頁面資料時錯誤: {ex}")
        return redirect(url_for('admin', tab='menu'))
    finally:
        cursor.close()
        conn.close()

@app.route('/add_ocr_menu_item', methods=['GET', 'POST'])
def add_ocr_menu_item():
    if 'username' not in session:
        flash('請先登入。', 'error')
        return redirect(url_for('home'))

    conn = get_db_connection()
    cursor = conn.cursor()
    param_marker = '%s' if DB_TYPE == 'MYSQL' else '?'

    if request.method == 'POST':
        store_name = request.form.get('store_name')
        item_name = request.form.get('item_name')
        price_small = request.form.get('price_small')

        if not store_name or not item_name or not price_small:
            flash('店家名稱、品項名稱與小份價格為必填欄位。', 'error')
            return redirect(url_for('add_ocr_menu_item', store_name=store_name))

        validation_error = validate_ocr_menu_item_data(request.form)
        if validation_error:
            flash(validation_error, 'error')
            return redirect(url_for('add_ocr_menu_item', store_name=store_name))
        
        try:
            # 步驟 1: 查詢或建立此店家的 ocr_menu 紀錄
            if DB_TYPE == 'MYSQL':
                # MySQL 使用 LIMIT 1
                query = f"SELECT ocr_menu_id, store_id FROM ocr_menus WHERE store_name = {param_marker} LIMIT 1"
            else: # SQL_SERVER
                # SQL Server 使用 TOP 1
                query = f"SELECT TOP 1 ocr_menu_id, store_id FROM ocr_menus WHERE store_name = {param_marker}"
            
            cursor.execute(query, (store_name,))
            menu_row = cursor.fetchone()
            ocr_menu_id = None
            if menu_row:
                ocr_menu_id = menu_row[0]
            else:
                # 如果沒有 OCR 菜單紀錄，則建立一筆新的
                cursor.execute(f"SELECT store_id FROM stores WHERE store_name = {param_marker}", (store_name,))
                store_row = cursor.fetchone()
                store_id = store_row[0] if store_row else None

                current_time = datetime.now()
                fixed_user_id = 99999 # 使用與上傳功能相同的固定 user_id
                cursor.execute(f"""
                    INSERT INTO ocr_menus (store_name, store_id, user_id, upload_time) 
                    VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})
                """, (store_name, store_id, fixed_user_id, current_time))

                if DB_TYPE == 'MYSQL':
                    ocr_menu_id = cursor.lastrowid
                else: # SQL_SERVER
                    cursor.execute("SELECT @@IDENTITY AS id")
                    ocr_menu_id = cursor.fetchone()[0]

            # 步驟 2: 插入新的 OCR 菜單品項
            price_big = request.form.get('price_big') or None
            cursor.execute(f"""
                INSERT INTO ocr_menu_items (ocr_menu_id, item_name, price_big, price_small) 
                VALUES ({param_marker}, {param_marker}, {param_marker}, {param_marker})
            """, (ocr_menu_id, item_name, price_big, price_small))
            
            if DB_TYPE == 'MYSQL':
                new_item_id = cursor.lastrowid
            else: # SQL_SERVER
                cursor.execute("SELECT @@IDENTITY AS id")
                new_item_id = cursor.fetchone()[0]

            # 步驟 3: 插入多語言翻譯
            lang_codes = request.form.getlist('lang_codes[]')
            descriptions = request.form.getlist('descriptions[]')
            if lang_codes and descriptions:
                for code, desc in zip(lang_codes, descriptions):
                    if code and desc:
                        cursor.execute(f"""
                            INSERT INTO ocr_menu_translations (menu_item_id, lang_code, description) 
                            VALUES ({param_marker}, {param_marker}, {param_marker})
                        """, (new_item_id, code, desc))

            conn.commit()
            flash(f"OCR品項 '{item_name}' 新增成功！", 'success')
            return redirect(url_for('admin', tab='ocr', store_name=store_name))
        except Exception as ex:
            conn.rollback()
            flash('新增OCR品項失敗，資料庫發生錯誤。', 'error')
            logging.error(f"新增 OCR 菜單品項時資料庫錯誤: {ex}")
            return redirect(url_for('add_ocr_menu_item', store_name=store_name))
        finally:
            cursor.close()
            conn.close()

    # 處理 GET 請求
    store_name = request.args.get('store_name')
    if not store_name:
        flash('未指定店家名稱。', 'error')
        return redirect(url_for('admin', tab='ocr'))
        
    try:
        # 取得所有可用語言
        cursor.execute("SELECT line_lang_code, lang_name, translation_lang_code FROM languages ORDER BY line_lang_code;")
        languages = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        
        store = {'store_name': store_name}

        return render_template('add_ocr_menu_item.html', store=store, languages=languages)
    except Exception as ex:
        flash('讀取頁面資料時發生錯誤。', 'error')
        logging.error(f"讀取新增OCR菜單頁面資料時錯誤: {ex}")
        return redirect(url_for('admin', tab='ocr'))
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(debug=True)
