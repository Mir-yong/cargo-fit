# Cargo Fit — 배포용 이미지
# Render / Railway / Fly.io / Hugging Face Spaces 어디서든 이 파일로 빌드된다.
#
# 로컬 맥에서는 wkhtmltopdf가 없어 작업명세서 PDF 다운로드만 실패하는데,
# 여기서는 이미지에 함께 설치하므로 배포본에서는 PDF까지 정상 동작한다.
FROM python:3.12-slim

# wkhtmltopdf + 한글 폰트(명세서 PDF에 한글이 들어간다)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wkhtmltopdf \
        fonts-nanum \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY clp/ ./clp/
COPY web/ ./web/
COPY server.py .
# 데모용 샘플 CSV (업로드할 파일이 없는 사람도 바로 시험해 볼 수 있게)
COPY modified_BR6_full.csv .

# 호스트가 주입하는 PORT를 server.py가 읽는다. 없으면 8000.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
