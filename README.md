# Enterprise WebOps Platform

阶段03已完成宿主机链路：web01-hz上systemd管理专用用户运行的FastAPI/Uvicorn，后端仅127.0.0.1:8000，Nginx提供IPv4 TCP80入口。Windows本地与Linux隔离环境均验证；容器化和自动发布尚未开始。

Linux服务配置见`nginx/`、`systemd/webops-app.service`；路径、日志、重启与排障见[宿主机服务手册](docs/runbooks/web-service.md)。运行源码版本为`23be76dc9b6a080aaf4e85a4a0a9809379a7f15b`，配置/文档提交独立于接口报告的源码SHA，验收见`docs/验收索引.md`。

## Windows 本地运行

需要 Python 3.12 和 Git。在项目根目录用 PowerShell 执行；无需激活虚拟环境或修改执行策略：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

若沙箱默认临时目录不可写，先设置当前进程的临时路径，再重试 venv（目录已被 Git 忽略）：

```powershell
New-Item -ItemType Directory -Force .local/tmp | Out-Null
$env:TEMP = Join-Path (Get-Location) '.local/tmp'
$env:TMP = $env:TEMP
```

另开终端核查，`curl.exe` 避免 PowerShell 同名别名；停止应用使用启动终端的 Ctrl+C：

```powershell
curl.exe -f http://127.0.0.1:8000/healthz
curl.exe -f http://127.0.0.1:8000/version
git diff --check
git status --short
git ls-files
```

接口：`/` 返回 `{"service":"webops-app"}`；`/healthz` 返回 `{"status":"ok"}`，只代表该进程可响应，无外部依赖；`/version` 返回 `{"version":"dev"}`（开发默认值）。不存在路径为404，错误方法为405；无公开故障注入入口。测试中的500仅由测试私有路由触发。

版本在进程启动时读取 `WEBOPS_VERSION`。后续发布应注入实际完整提交 SHA；空值、空格、换行及超过128字符会拒绝启动。当前支持字母、数字、点、下划线和连字符。变量只存当前进程环境，不写入源码或提交运行时文件：

```powershell
$env:WEBOPS_VERSION = git rev-parse HEAD
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 停止进程后移除当前终端中的变量
Remove-Item Env:WEBOPS_VERSION
```

## 依赖与边界

`requirements.txt` 锁定运行依赖及传递依赖；`requirements-dev.txt` 引用它并锁定 pytest/httpx 等测试依赖。Python3.12/Windows和阶段03 CentOS Stream10/Python3.12.14均已安装、pip check和9测试通过（1条TestClient弃用警告），真实HTTP另有实测。以下命令适合另建隔离环境；不要在正在运行的发布目录中临时改依赖：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

不设置Git远端、不公开推送；`.local/`、虚拟环境、凭据、真实inventory及运行参数均不得跟踪。原有规划和历史证据保留在原处；提交采用明确文件清单，不把未审查历史目录整树加入Git。阶段03因云端PyPI解析失败/下载慢，使用官方同版本Linux wheel，核对关键wheel官方SHA256及传输归档SHA256后离线安装，再逐项核对全部锁版本与目标Linux测试；未复制Windows虚拟环境、未升级锁。细节见当日日志09。

依赖/测试方式参考 [FastAPI运行文档](https://fastapi.tiangolo.com/deployment/manually/) 和 [FastAPI测试文档](https://fastapi.tiangolo.com/tutorial/testing/)。本项目只使用三个JSON接口，不启用文档页面或数据库。
