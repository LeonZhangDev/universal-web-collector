
# Universal Web Collector 项目介绍

## 项目定位

Universal Web Collector 是一个通用网页资源采集平台。

目标不是简单爬虫，而是构建一个可扩展的 Resource Extraction Agent 基础设施：

网页 URL
→ 浏览器环境
→ 资源发现
→ 资源标准化
→ 下载任务
→ 存储管理


## 核心能力

当前版本：

- uv 管理 Python 环境
- Makefile 统一启动
- FastAPI 后端
- Vue3 前端预留
- Playwright 浏览器自动化
- Network Response 资源监听
- 图片资源发现
- 视频资源发现
- Resource 统一模型


## 主要使用场景

1. 相册采集

例如 XChina:

photoShow.html?id=xxx

自动解析页面加载资源。


2. 视频采集

支持发现:

- mp4
- m3u8


3. 通用网页资源采集

未来支持:

- 图片
- 视频
- PDF
- 文档
- 音频
- 网页文本


## 架构思想

Collector 不负责下载细节。

Collector:
负责发现资源。

Downloader:
负责下载资源。


统一流程：

URL
 |
Browser
 |
Extractor
 |
Resource
 |
Downloader
 |
Storage
