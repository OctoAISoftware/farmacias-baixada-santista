import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica Neue",
          "Arial",
          "sans-serif"
        ]
      },
      colors: {
        brand: {
          50: "#eef9f6",
          100: "#d8f1e9",
          200: "#b3e3d2",
          300: "#7ccbb1",
          400: "#3faf8d",
          500: "#1f9270",
          600: "#14755a",
          700: "#0f5d49",
          800: "#0c4a3c",
          900: "#093b30"
        },
        accent: {
          50: "#fff6ed",
          100: "#ffe9d2",
          200: "#fed1a8",
          300: "#fdb173",
          400: "#fb8a3c",
          500: "#f56d18",
          600: "#e2530c",
          700: "#bb3f0c",
          800: "#953311",
          900: "#782b10"
        },
        ink: {
          50: "#f6f7f9",
          100: "#eceef3",
          200: "#d5dae5",
          300: "#b1bbcd",
          400: "#8794b0",
          500: "#677496",
          600: "#525d7b",
          700: "#444c63",
          800: "#3a4051",
          900: "#1f2433",
          950: "#0e121b"
        }
      },
      boxShadow: {
        soft: "0 6px 18px -8px rgba(15, 23, 42, 0.18)",
        ring: "0 0 0 4px rgba(31, 146, 112, 0.18)"
      }
    }
  },
  plugins: []
} satisfies Config;