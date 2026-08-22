FROM python:3.11-slim

ENV DAGSTER_HOME=/opt/dagster/app
WORKDIR /opt/dagster/app

# Install deps first for layer caching; copy package metadata only
COPY pyproject.toml ./
RUN mkdir -p hidden_stock && echo "# placeholder" > hidden_stock/__init__.py \
  && pip install --no-cache-dir . \
  && rm -rf hidden_stock

COPY hidden_stock ./hidden_stock
COPY dagster.yaml workspace.yaml ./
RUN pip install --no-cache-dir --no-deps .

EXPOSE 3000 4000
