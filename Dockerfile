# 使用 Python 3.12 的 Debian Bullseye 映像檔以獲得更好的相容性
FROM python:3.12-bullseye

# 設定環境變數，防止 Python 寫入 .pyc 檔案
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# --- 安裝 Microsoft ODBC Driver for SQL Server (相容性修正版) ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gnupg \
        ca-certificates \
        curl \
        unixodbc-dev && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/11/prod bullseye main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 && \
    # 清理 apt 快取以縮小映像檔體積
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
# ---------------------------------------------------------

# 設定工作目錄
WORKDIR /app

# 複製套件清單檔案到容器中
COPY requirements.txt .

# 安裝所有 Python 套件
RUN pip install --no-cache-dir -r requirements.txt

# 複製您專案的所有檔案到容器中
COPY . .

# 開放容器的 8080 連接埠
EXPOSE 8080

# 容器啟動時要執行的指令
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "3", "app:app"]
