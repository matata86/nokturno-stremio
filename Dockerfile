# Doplněk nemá žádné závislosti mimo standardní knihovnu, takže stačí slim obraz
# a zkopírovat zdrojáky. Žádný pip install, žádný build.
FROM python:3.12-slim

# vlastní uživatel: doplněk nic nepotřebuje pod rootem a sahá jen do /data
RUN useradd --create-home --uid 10001 nokturno
WORKDIR /app

COPY nokturno/ ./nokturno/
COPY README.md LICENSE ./

# cache jádra (katalogy, metadata, nalezené streamy) — bez svazku se po každém
# restartu tahá znovu, viz docker-compose.yml
ENV NOKTURNO_DATA=/data \
    NOKTURNO_PORT=7127 \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown nokturno:nokturno /data
VOLUME ["/data"]
USER nokturno
EXPOSE 7127

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request,os,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('NOKTURNO_PORT','7127')+'/health', timeout=4).status == 200 else 1)"

CMD ["python3", "-m", "nokturno.server"]
