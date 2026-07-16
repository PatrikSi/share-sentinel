FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build
WORKDIR /app
ARG VITE_API_BASE_URL=/api
ARG VITE_CSRF_COOKIE_NAME=share_sentinel_csrf
ARG VITE_CSRF_HEADER_NAME=x-csrf-token
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_CSRF_COOKIE_NAME=${VITE_CSRF_COOKIE_NAME} \
    VITE_CSRF_HEADER_NAME=${VITE_CSRF_HEADER_NAME}
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install --no-audit --no-fund; fi
COPY . .
RUN npm run build

FROM nginx:1.31-alpine@sha256:7068961d45b07b2af510ac002e9daa63a1d3eba2111202d6768798690800fffd
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
