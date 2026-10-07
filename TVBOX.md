# TVBox 自动聚合订阅源（tvbox-airport）

本目录是「TVBox 自动聚合订阅源」，与仓库里的代理订阅（`generator.py` / `update.yml`）**相互独立**，互不干扰。

## 订阅地址（国内直连）

```
https://cdn.jsdelivr.net/gh/mantisli/free-proxy-airport@main/tvbox.json
```

## 它做什么

GitHub Actions 每 6 小时自动跑一次 `tvbox_generator.py`：

1. 从自动维护的「接口清单文档」动态抽取候选点播源 + 直播源
2. 并发探测每个候选是否真的可用（点播 = 合法 TVBox 配置 JSON，直播 = 含流的 m3u）
3. 合并去重、修复相对路径、给每个 csp_ 站点挂回它来源的 spider jar
4. 输出 `tvbox.json` + `STATUS.md` 并自动提交
5. **自愈**：某轮点播源全挂时保留上一版可用配置，订阅不会中断；首次运行无历史配置时回落到内置保底配置

## 相关文件

| 文件 | 作用 |
|------|------|
| `tvbox_generator.py` | 生成器（纯标准库，无需 pip） |
| `.github/workflows/tvbox-update.yml` | 每 6 小时 + 手动触发的自动更新工作流 |
| `tvbox.json` | 生成产物（订阅地址指向它） |
| `STATUS.md` | 运行报告：哪些源活着、哪些死了 |
| `TVBOX.md` | 本说明 |

## 手动更新

仓库 **Actions → TVBox Self-Healing Source → Run workflow** 手动触发一次。

## 边界声明

本项目**不存储、不分发**任何影视内容，只做公开配置接口的可用性探测与聚合。
