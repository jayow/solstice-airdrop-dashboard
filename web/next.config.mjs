/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export - main page is prerendered; wallet detail is a single
  // client-side template that fetches public/events/<addr>.json on demand.
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: true,
};
export default nextConfig;
