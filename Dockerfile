FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY data_broker ./data_broker
COPY README.md LICENSE ./
# access-broker-core is vendored at image build time (path dependency
# until GitHub publication; the owner publishes the suite separately).
# Build with the core directory adjacent so uv resolves the editable
# path dependency, then pin.

RUN pip install --no-cache-dir .

RUN useradd -m -u 1000 databroker
USER databroker

EXPOSE 8471

ENTRYPOINT ["python", "-m", "data_broker.run"]