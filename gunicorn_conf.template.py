# Copy this file to gunicorn_conf.py after deployment and replace the example paths.

# Project directory
chdir = '/path/to/bilibili_danmaku'

# 指定进程数
workers = 4

# 指定每个进程开启的线程数
threads = 2

#启动用户
user = 'www'

# 启动模式
worker_class = 'gthread'

# 绑定的ip与端口
bind = '127.0.0.1:5000'

# Process pid file
pidfile = '/path/to/runtime/gunicorn.pid'

# Access and error logs
accesslog = '/path/to/logs/gunicorn_access.log'
errorlog = '/path/to/logs/gunicorn_error.log'

# 日志级别，这个日志级别指的是错误日志的级别，而访问日志的级别无法设置
# debug:调试级别，记录的信息最多；
# info:普通级别；
# warning:警告消息；
# error:错误消息；
# critical:严重错误消息；
loglevel = 'info' 

# Keep synchronous AI requests below uWSGI harakiri parity.
timeout = 60

# 自定义设置项请写到该处
# 最好以上面相同的格式 <注释 + 换行 + key = value> 进行书写， 
# PS: gunicorn 的配置文件是python扩展形式，即".py"文件，需要注意遵从python语法，
# 如：loglevel的等级是字符串作为配置的，需要用引号包裹起来
