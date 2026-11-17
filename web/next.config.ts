import type { NextConfig } from "next";

// Portfolio images are served from this project's Supabase storage bucket.
const supabaseHost = (() => {
  try {
    return new URL(process.env.NEXT_PUBLIC_SUPABASE_URL ?? "").hostname;
  } catch {
    return "tmswmgwomoabvftdreeb.supabase.co";
  }
})();

const nextConfig: NextConfig = {
  // BlockNote's server renderer pulls in a DOM implementation that must not be
  // bundled (copied from ../doctor-portfolio).
  serverExternalPackages: ["@blocknote/server-util"],
  experimental: {
    // Server Actions are capped at 1 MB by default, and a write-up saves as
    // one action. A page pasted from a word processor arrives with its images
    // inline, blows past that, and the action is rejected before it reaches
    // the database — which is what an operator hit on 2026-09-21. The editor
    // now uploads pasted images (components/editor/BlockEditor.tsx), so a
    // document should stay small; this is headroom for the moment between the
    // paste and the upload finishing, not a licence to store images in prose.
    serverActions: { bodySizeLimit: "4mb" },
  },
  images: {
    remotePatterns: [
      { protocol: "https", hostname: supabaseHost, pathname: "/storage/v1/object/public/**" },
      // Poster frames for video albums (lib/video.ts youtubeThumb).
      { protocol: "https", hostname: "i.ytimg.com", pathname: "/vi/**" },
    ],
  },
};

export default nextConfig;
