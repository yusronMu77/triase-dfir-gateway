# syntax=docker/dockerfile:1.10
# Jembatan WhatsApp Web (Baileys) untuk uji Astria Gateway. Bukan untuk server privat (lihat README).
ARG NODE_IMAGE=node:24.22.2-bookworm-slim

FROM ${NODE_IMAGE} AS deps
WORKDIR /app
COPY package.json package-lock.json .npmrc ./
# .npmrc: ignore-scripts (tanpa skrip instal pihak ketiga) dan legacy-peer-deps (sharp/jimp tidak dipasang).
RUN npm ci --omit=dev && npm cache clean --force

FROM ${NODE_IMAGE} AS runtime
ENV NODE_ENV=production \
    WA_BRIDGE_HOST=0.0.0.0 \
    WA_BRIDGE_PORT=3000 \
    WA_BRIDGE_DATA_DIR=/data
RUN install -d -o node -g node -m 0700 /data
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src
USER node
EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/status').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "src/main.js"]
