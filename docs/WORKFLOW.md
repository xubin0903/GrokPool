# 日常开发 / 发行工作流

## 分支

| 分支 | 用途 | 是否推送 |
|------|------|----------|
| `main` | 公开发行分支（GitHub public） | 要推 |
| `local` | 你本机日常开发、联调、试验 | **默认不推** |

## 日常

```bat
git checkout local
:: 改代码、跑注册机、起 Docker、测试
git add ...
git commit -m "..."
```

个人运行时文件继续放本地即可（已被 gitignore）：

- `deploy/.env`
- `register-win/config.json`
- `register-win/token.json`
- `register-win/mail_credentials.txt`
- `register-win/accounts_*.txt`
- `register-win/data/cpa/*`

**不要** `git add -f` 强行加入这些文件。

## 发行到公开 main

确认 `local` 上功能 OK、且没有个人密钥进提交后：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\promote-to-main.ps1
```

脚本会：

1. 检查当前在 `local`
2. 拒绝已跟踪的密钥路径
3. `main` 快进拉取
4. merge `local` → `main`
5. 再扫一遍发行 diff
6. `git push origin main`
7. 切回 `local`

## 手动等价流程

```bat
git checkout main
git pull origin main
git merge local
git push origin main
git checkout local
```

## 注意

- 不要把 `local` 设成默认推送分支去覆盖 public 历史
- Docker 镜像构建始终以你当前检出的源码为准：`scripts\build-sub2api.ps1`
