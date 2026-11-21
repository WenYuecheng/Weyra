FROM node:22.14.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/py && /opt/py/bin/pip install --no-cache-dir pytest==8.3.5
ENV PATH="/opt/py/bin:/opt/node/node_modules/.bin:${PATH}" NODE_PATH=/opt/node/node_modules
RUN mkdir /opt/node && cd /opt/node && npm init -y && npm install --save-exact jest@29.7.0 babel-jest@29.7.0 @babel/core@7.25.2 @exercism/babel-preset-javascript@0.2.1 core-js@3.37.1
COPY worker.py /opt/worker.py
RUN chmod 0444 /opt/worker.py
USER 1000:1000
WORKDIR /work
