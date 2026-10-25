import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // cacheComponents включает Cache Components в Next 16.
  // partialPrefetching не является документированной опцией Next 16 — удалён,
  // иначе ключ молча игнорируется и создаёт ложное впечатление оптимизации.
  cacheComponents: true,
};

export default nextConfig;
