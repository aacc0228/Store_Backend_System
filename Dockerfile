# 使用官方的 Python 3.11 slim 映像檔作為基礎
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 複製套件清單檔案到容器中
COPY requirements.txt requirements.txt

# 安裝所有必要的套件
RUN pip install --no-cache-dir -r requirements.txt

# 複製您專案的所有檔案到容器中
COPY . .

# 設定環境變數 (可選，但建議)
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# 開放容器的 8080 連接埠
EXPOSE 8080

# 容器啟動時要執行的指令
# 使用 gunicorn 作為正式環境的 WSGI 伺服器，而不是 Flask 內建的開發伺服器
# --bind 0.0.0.0:8080 讓容器可以從外部存取
# --workers 3 是一個建議的起始值
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "3", "app:app"]
