#!/usr/bin/env python3
"""git push时如果需要用户名/密码，会调用GIT_ASKPASS环境变量指向的这个脚本，
把它的标准输出当作答案——这是git原生支持、专门为"脚本化推送但不想让密码
出现在URL/命令行参数/git config里"这个场景设计的机制，不是本项目发明的技巧。

真正的token只从FOXZEN_GIT_PUSH_TOKEN这个环境变量读取（由调用方在subprocess
的env里临时设置，见git_publish.py），本文件本身不含任何密钥，可以安全地
进入版本库、被任何人读取。

git对HTTPS远程会分别用不同的提示语调用这个脚本问用户名和密码（例如
"Username for 'https://github.com': "和"Password for 'https://x@github.com': "）。
对GitHub的PAT认证而言，用户名填什么都不影响认证结果（GitHub只校验密码位置
的token），所以这里对两种提示统一回答同一个token值，不解析argv里具体是
问用户名还是密码。
"""
import os
import sys

if __name__ == "__main__":
    sys.stdout.write(os.environ.get("FOXZEN_GIT_PUSH_TOKEN", ""))
