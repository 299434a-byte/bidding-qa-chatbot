/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Next 16 默认只放行 localhost 访问开发资源；用 127.0.0.1 打开时
  // /_next 请求会被拦截，导致客户端 JS 不加载、页面无法交互。
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default nextConfig;