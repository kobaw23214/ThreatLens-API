"""TI 联网情报模块（微步在线 / 奇安信）。

定位：纯情报查询展示服务——通过浏览器自动化（Playwright）登录情报平台
查询 IP/域名/样本，把平台页面原生内容（sections + 原始 JSON）取回展示，
不做扩线等业务加工。采集规则外置在 selectors/{platform}.yml。
"""
