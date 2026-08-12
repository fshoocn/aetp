import pluginVue from "eslint-plugin-vue";
import vueTsEslintConfig from "@vue/eslint-config-typescript";
import skipFormatting from "@vue/eslint-config-prettier/skip-formatting";

export default [
  {
    name: "app/files-to-lint",
    files: ["**/*.{ts,mts,tsx,vue}"],
  },
  {
    name: "app/files-to-ignore",
    ignores: ["**/dist/**", "**/node_modules/**", "**/coverage/**"],
  },
  ...pluginVue.configs["flat/essential"],
  ...vueTsEslintConfig(),
  skipFormatting,
  {
    name: "app/overrides",
    rules: {
      // 视图文件名为单次（Dashboard/Login 等），禁用多词组件名约束
      "vue/multi-word-component-names": "off",
    },
  },
];
