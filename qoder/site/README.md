# site/ · Qoder Sites 发布源

CiteFlow 项目官网的静态源文件。无构建步骤、无外部依赖、无密钥：
`index.html` + `site.css` + `site.js` + 真实截图（`assets/`）+ 使用快照（`data/usage.json`）。

## 本地预览

```bash
cd qoder
python3 -m http.server 8787 --directory site
# 打开 http://127.0.0.1:8787/
```

已用 Playwright（Chromium 1187）完成桌面 1440px 与移动 390px 全量检查：
控制台无错误、资源全部 200、无横向溢出、标签页/灯箱/复制/锚点/移动端导航交互正常。

## 发布到 Qoder Sites（账号持有人操作）

1. 在 Qoder（个人账号，企业版无法设置公开访问）中打开 **Sites**，新建站点。
2. 上传/选择本目录 `qoder/site/` 作为站点源（纯静态，无需构建命令）。
3. 发布后把可见范围设置为 **「任何人都可访问」**。
4. 用**无痕窗口（未登录状态）**打开站点 URL，确认可以访问，再回填提交材料。
5. 将发布后的完整 URL 记录到 `topics/eco-03-qoder-citeflow/05-submit.md`。

检查清单：

- [ ] 未登录（无痕窗口）可打开站点 URL
- [ ] 页面图片、`data/usage.json` 全部加载（无 404）
- [ ] 站点内 GitHub 链接指向已推送的仓库分支
- [ ] 站点不含 UID、令牌、内网地址等敏感信息（UID 只出现在提交材料中）

## 数据与截图刷新

站点展示的是开发期真实运行快照，标注了采集时间与来源。重新采集：

```bash
# 1. 启动应用（见项目 README 的 Quickstart），然后导出使用快照
.venv/bin/python scripts/export_site_data.py          # 写入 site/data/usage.json

# 2. 重新截图后压缩为 WebP，替换 site/assets/ 下同名文件
.venv/bin/python scripts/capture_screenshots.py
```

`usage.json` 只包含查询次数、延迟分布、文档规模、模式计数与近 4 条查询文本，
不包含任何用户信息或凭据。
