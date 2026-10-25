# WhyCode 前端镜像:Vue build → Nginx 静态资源 + /api 反代
# build context 为仓库根(docker-compose context: .),以复用根级 nginx.conf
FROM node:20-alpine AS build
WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginx:alpine
# Nginx 配置:静态资源 + /api 反代(SSE 关闭缓冲)
COPY nginx.conf /etc/nginx/conf.d/default.conf
# 把 Vue build 产物拷入 Nginx 根
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
