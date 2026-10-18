/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#F7F5F2",
        forest: {
          DEFAULT: "#16302B",
          light: "#234A41",
        },
        gold: {
          DEFAULT: "#C9A227",
          light: "#DCC066",
        },
        ink: "#1F2421",
        sage: "#3C7A5D",
        brick: "#B0463C",
      },
      fontFamily: {
        display: ["Fraunces", "serif"],
        body: ["Inter", "sans-serif"],
        mono: ["IBM Plex Mono", "monospace"],
      },
    },
  },
  plugins: [],
};
