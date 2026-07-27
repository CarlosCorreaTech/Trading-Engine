# The whole assessment in one image: warehouse, engine, tests, notebook and the
# demo console.
#
# The warehouse is built during the image build rather than on first request, so
# the container starts with its data already in place. It only takes a few
# seconds either way, but doing it here means a broken SQL model fails the image
# build rather than at demo time.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first and alone, so editing a SQL model or a detector does not
# reinstall scipy.
COPY requirements.txt .

# --only-binary fails immediately and legibly if a wheel is ever missing for
# this Python. Without it pip falls back to building from source, which on a
# slim image means several minutes of compiling before failing anyway on the
# absent toolchain.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY data/ data/
COPY sql/ sql/
COPY src/ src/
COPY app/ app/
COPY tests/ tests/
COPY notebooks/ notebooks/
COPY pyproject.toml README.md ./

RUN python -m src.build --quiet

# Nothing here needs root, and the warehouse is already built. Ownership is
# handed over anyway so that a rebuild inside a running container works.
RUN useradd --create-home --uid 1000 engine && chown -R engine:engine /app
USER engine

EXPOSE 8000

# The readiness probe asks the API, not the port. The pipeline runs during
# startup, so a container that is listening is not yet a container that can
# answer, and reporting healthy early would just move the error to the browser.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/overview', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
