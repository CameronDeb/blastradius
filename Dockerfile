FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --disable-pip-version-check -r /app/requirements.lock

# sqlglot at the pinned commit
RUN python -c "import io,tarfile,urllib.request; \
u='https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
t=tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(u).read())); t.extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot \
 && python -m compileall -q -j 0 /opt/sqlglot/sqlglot /opt/sqlglot/tests \
 && chmod -R a+rX /opt/sqlglot

COPY selector.py map.json.gz /app/
RUN python -m compileall -q /app && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_MAX_THREADS=1 NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1
CMD ["python", "/app/selector.py"]
