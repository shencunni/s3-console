# S3 / Object Index

一个本地 S3 控制台第一版，参考 `crawler_discover/web` 的 React + TypeScript + Vite 结构与深色数据控制台视觉。当前只覆盖：

- S3 连接档案管理：保存 Endpoint、Region、Bucket、Access Key ID 等非秘密元数据
- 连接建立与 `HeadBucket` 验证
- 按前缀查询目录与对象列表
- 对象 Key 的单个删除（带 bucket + 完整 Key 确认）
- 目录前缀的递归删除（先扫描数量与示例 Key，再确认执行；不允许清空整个 Bucket）
- 在同一个工作区切换“前缀浏览 / 下载任务”菜单
- 下载任务按当前连接隔离显示；切换连接不会误显示旧任务，旧任务仍在后端后台继续执行
- 一级目录浏览、递归列表、分页加载、复制对象 Key

Secret Access Key 和 Session Token 只在建立当前连接时发送给本地 Python API，保存在后端进程内存中，不写入配置档案。

## 启动

终端一：启动本地 S3 API。复用当前已有的 S3 虚拟环境：

```bash
cd s3-console
python3 server.py
```

终端二：启动 React 开发页面：

```bash
cd s3-console/web
npm install
npm run dev
```

打开 [http://localhost:5173](http://localhost:5173)。Vite 会把 `/api` 请求转发到 `127.0.0.1:8765`。

也可以构建后由 Python API 直接提供页面：

```bash
npm run build
cd ..
python3 server.py
```

然后打开 [http://localhost:8765](http://localhost:8765)。

## 删除

- 对象行右侧的 `×` 删除单个 Key。
- 目录行右侧的 `×` 递归删除该目录前缀下的所有对象。
- 也可以在前缀输入框输入 `code/` 或 `code/repos`，查询后点击 `DELETE PREFIX ×`。
- 目录删除会先扫描并显示对象数量、总大小和示例 Key，再等待确认；网页不允许把空前缀作为删除范围来清空整个 Bucket。

## 配置文件

保存后的连接元数据在 `s3-console/.s3_console/profiles.json`，该目录已被 `.gitignore` 排除，并且文件权限会设置为仅当前用户可读写。Secret Key 和 Session Token 不会写入此文件。

后端日志会同时显示在启动终端，并写入 `s3-console/.s3_console/server.log`。日志只记录接口、连接目标、查询前缀、返回数量和错误类型，不记录 Secret Key 或 Session Token。

## 下载

连接成功后：

- 对象行右侧的 `↓` 下载单个文件
- 目录行右侧的 `↓ ZIP` 下载整个前缀目录
- 下载任务会显示扫描、传输进度、失败或完成状态；完成后点击 `DOWNLOAD ↓` 保存到本地

后端把中间文件写入 `s3-console/.s3_console/downloads/`，单文件直接流式写入，目录则边读取 S3 对象边创建 ZIP，不把完整内容放进内存。任务和临时文件默认保留 1 小时后清理；目录下载限制为最多 50,000 个对象、50 GB。切换连接不会自动取消旧任务，只会将其从当前列表中隔离，并提示它仍在后台执行。
