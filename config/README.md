# config/ —— 本机运行配置（凭据与状态）

本目录存放**只属于这台机器**的运行配置，全部已在仓库根 `.gitignore` 中，**不会被提交**。

| 文件 | 作用 | 说明 |
|---|---|---|
| `.env.mubu` | 凭据 | `MUBU_PHONE` / `MUBU_PASSWORD` / `MUBU_MEMBER_ID`，**明文** |
| `.mubu_token` | Token 缓存 | 自动生成、约 2 小时过期自动重登，可随时删除 |
| `.mubu_trash.json` | 软删除回收站标记 | 用过 `delete`（软删除）后才出现 |
| `*.lock` | 跨进程写锁 | 运行时临时文件 |

## 解析顺序（`scripts/mubu/config.py`）

1. 环境变量 `MUBU_CONFIG_DIR` / `MUBU_ENV_FILE` / `MUBU_TOKEN_FILE` / `MUBU_TRASH_FILE`
2. **本目录**（`<repo>/config/`）
3. 旧主目录路径 `~/.workbuddy/.env.mubu`、`~/.mubu_token`（仅为兼容老安装保留）

## 打包 / 分享前必读

- 本目录是**明文密码**。`git` 不会提交它，但**普通压缩、拷贝、上传网盘会把它一起带走**。
- 对外发布、把仓库打包给别人之前：**先删除或排除整个 `config/` 目录**。
- 需要连配置一起迁移时，走加密通道；或只带代码，让对方自己填凭据。
