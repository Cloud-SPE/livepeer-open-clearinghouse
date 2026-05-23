// ESLint v9 flat config. typescript-eslint's strict-type-checked preset
// turns on the rules that catch real bugs (no-floating-promises,
// no-misused-promises, no-unnecessary-condition) without too much
// stylistic opinion. Prettier handles formatting; ESLint stays out of
// its way.

import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["node_modules/**", "dist/**", "coverage/**", "*.config.ts", "*.config.js"],
  },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  ...tseslint.configs.stylisticTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        project: "./tsconfig.json",
        tsconfigRootDir: import.meta.dirname,
      },
      globals: { ...globals.node },
    },
    rules: {
      // We use a couple of `unknown` shapes for response decoding;
      // the type assertions are scoped + commented.
      "@typescript-eslint/no-explicit-any": "warn",
      // Test files have legitimate void promise calls in mocks.
      "@typescript-eslint/no-confusing-void-expression": ["error", { ignoreArrowShorthand: true }],
    },
  },
  {
    // Tests can throw a few stylistic rules out the window.
    files: ["tests/**/*.ts"],
    rules: {
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
      "@typescript-eslint/no-unsafe-argument": "off",
      "@typescript-eslint/no-unsafe-return": "off",
      "@typescript-eslint/no-unsafe-call": "off",
      "@typescript-eslint/no-misused-promises": "off",
    },
  },
);
