FROM node:20.20.2-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install --no-audit --no-fund; fi
COPY . .
RUN npm run build

FROM nginx:1.31.3-alpine
RUN addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app app
COPY nginx-main.conf /etc/nginx/nginx.conf
COPY --chown=10001:10001 nginx.conf /etc/nginx/conf.d/default.conf
COPY --chown=10001:10001 --from=build /app/dist /usr/share/nginx/html
COPY --chown=10001:10001 docker-entrypoint.d/40-runtime-config.sh /docker-entrypoint.d/40-runtime-config.sh
RUN mkdir -p \
      /var/cache/nginx/client_temp \
      /var/cache/nginx/proxy_temp \
      /var/cache/nginx/fastcgi_temp \
      /var/cache/nginx/uwsgi_temp \
      /var/cache/nginx/scgi_temp \
    && chown -R 10001:10001 /var/cache/nginx /usr/share/nginx/html
USER 10001:10001
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
