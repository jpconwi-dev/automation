/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#0e0e11", 900: "#141418", 800: "#1b1b21", 700: "#26262e", 600: "#34343f" },
        brand: { pink: "#ff4d9d", magenta: "#d946ef", purple: "#8b5cf6" },
      },
      fontFamily: {
        display: ["'Bricolage Grotesque'", "ui-sans-serif", "system-ui", "sans-serif"],
        sans: ["'Instrument Sans'", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
