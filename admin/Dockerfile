FROM node:22-alpine AS admin
WORKDIR /src
COPY admin/package.json ./
RUN npm install
COPY admin .
RUN npm run build

FROM node:22-alpine AS mini
WORKDIR /src
COPY miniapp/package.json ./
RUN npm install
COPY miniapp .
RUN npm run build

FROM nginx:1.27-alpine
COPY admin/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=admin /src/dist /usr/share/nginx/html
COPY --from=mini /src/dist /usr/share/nginx/mini
