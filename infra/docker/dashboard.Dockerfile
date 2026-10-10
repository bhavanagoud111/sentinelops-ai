# syntax=docker/dockerfile:1.7
FROM node:24.21.0-alpine AS build
WORKDIR /app
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY apps/dashboard/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31.6-alpine
COPY infra/docker/dashboard-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
