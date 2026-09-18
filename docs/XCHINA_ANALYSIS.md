
# XChina Collector设计说明


## 当前页面模式

旧方式:

album_id

拼接:

001.jpg
002.jpg


已经不推荐。


新版:

photoShow.html?id=xxx


应该通过浏览器加载后发现真实资源。


## 推荐解析链路


Playwright

↓

监听 Response

↓

过滤:

image/*
video/*


↓

Resource对象



## Resource格式


{
 type:image,

 url:

 headers:

}


视频:

{
 type:video,

 url:m3u8,

 headers:
}


## 为什么保存headers

CDN资源经常依赖:

- Cookie
- Referer
- User-Agent


下载时必须携带。


## 后续扩展

增加:

quality selector

选择:

original

large

medium

thumbnail


避免下载低质量缩略图。
