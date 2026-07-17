# Right Code gpt-image-2 本地图片工具

一个只使用 Python 标准库的命令行工具。它可以向 Right Code 提交异步图片任务，也可以根据 Cherry Studio 或后台中已有的 `task_id` 查询结果并下载到本地。

## 环境与配置

- Python 3.9 或更高版本
- 可用的 Right Code API Key

复制配置示例并填写密钥：

```powershell
Copy-Item config.example.json config.json
```

`config.json` 内容如下：

```json
{
  "api_key": "sk-你的密钥"
}
```

该文件已加入 `.gitignore`。如需将配置放到其他位置，可在命令中使用 `--config D:\path\config.json`。

## 生成并下载

最小用法：

```powershell
python .\rc_image.py generate --prompt "一只戴着太空头盔的橘猫，电影级光影"
```

指定尺寸、分辨率和数量：

```powershell
python .\rc_image.py generate `
  --prompt "未来城市雨夜海报" `
  --size 16:9 `
  --image-size 2K `
  --n 2
```

使用一张或多张本地参考图：

```powershell
python .\rc_image.py generate `
  --prompt "参考主体，改成赛博朋克杂志封面" `
  --image .\reference-1.png `
  --image .\reference-2.jpg
```

工具默认使用模型 `gpt-image-2`，向生成接口发送 `async: true`，取得 `task_id` 后持续轮询，最终保存到：

```text
output/<task_id>/image_001.png
```

## 抓取已有任务

从 Cherry Studio 的返回内容或 Right Code 异步任务后台复制任务 ID：

```powershell
python .\rc_image.py fetch task_0123456789abcdef0123456789abcdef
```

官方公开接口只支持按 `task_id` 查询，不能通过 API 自动列出 Cherry 创建的全部任务。因此必须先取得具体的任务 ID。

## 常用选项

```text
--output-dir PATH     修改输出根目录
--interval SECONDS   轮询间隔，默认 3 秒
--timeout SECONDS    总等待时间，默认 600 秒
--http-timeout SEC   单次 HTTP 超时，默认 60 秒
--overwrite          覆盖已存在的图片
```

查看完整参数：

```powershell
python .\rc_image.py --help
python .\rc_image.py generate --help
python .\rc_image.py fetch --help
```

## 接口说明

- 提交：`POST https://www.right.codes/draw/v1/images/generations`
- 查询：`GET https://www.right.codes/v1/tasks/{task_id}`
- 鉴权：`Authorization: Bearer <API_KEY>`

工具支持任务结果中的图片 URL、`b64_json`、`base64`，并兼容官方示例中完成响应不包含 `status` 的情况。下载图片时不会把 API Key 转发给 CDN 地址。
