FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY data_broker ./data_broker
COPY README.md LICENSE ./
# access-broker-core is vendored at image build time (path dependency
# until GitHub publication; the owner publishes the suite separately).
# The build context must place the core at ../accessBrokerCore relative
# to this repo, and the build REQUIRES a uv-capable environment: the
# editable path dependency resolves through uv/pip's PEP 660 editable
# hooks with the core on disk (DEPLOYMENT.md §7 rollback path).
# pip alone will fail the path dependency resolution without the core
# present in the build stage.

RUN pip install --no-cache-dir .

RUN useradd -m -u 1000 databroker
USER databroker

EXPOSE 8471

ENTRYPOINT ["python", "-m", "data_broker.run"]