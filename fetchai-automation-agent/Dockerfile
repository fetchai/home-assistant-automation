FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_home/ .
COPY proxy.py /proxy.py
COPY panel/dist /panel/dist
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
