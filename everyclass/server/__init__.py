import gc
import sys

import logbook
from flask import Flask, g, render_template, request, session
from flask_cdn import CDN
from flask_recaptcha import ReCaptcha
from flask_session import Session
from htmlmin import minify
from raven.contrib.flask import Sentry
from raven.handlers.logbook import SentryHandler

from everyclass.server.utils import monkey_patch

logger = logbook.Logger(__name__)
sentry = Sentry()
__app = None
__first_spawn = True

recaptcha = ReCaptcha()

try:
    import uwsgidecorators

    """
    below are functions that will be executed in **each** process after fork().
    these functions will be executed in the same order of definition here.
    """

    @uwsgidecorators.postfork
    def enable_gc():
        """enable garbage collection"""
        gc.set_threshold(700)

    @uwsgidecorators.postfork
    def init_log_handlers():
        """init log handlers and print current configuration to log"""
        from everyclass.server.utils.logbook_logstash.handler import LogstashHandler
        from elasticapm.contrib.flask import ElasticAPM
        from everyclass.server.config import print_config
        ElasticAPM.request_finished = monkey_patch.ElasticAPM.request_finished(ElasticAPM.request_finished)

        global __app, __first_spawn

        # Elastic APM
        if __app.config['CONFIG_NAME'] in __app.config['APM_AVAILABLE_IN']:
            ElasticAPM(__app)
            logger.info('APM is inited because you are in {} mode.'.format(__app.config['CONFIG_NAME']))

        # Logstash centralized log
        if __app.config['CONFIG_NAME'] in __app.config['LOGSTASH_AVAILABLE_IN']:
            logstash_handler = LogstashHandler(host=__app.config['LOGSTASH']['HOST'],
                                               port=__app.config['LOGSTASH']['PORT'],
                                               release=__app.config['GIT_DESCRIBE'],
                                               bubble=True,
                                               logger=logger,
                                               filter=lambda r, h: r.level >= 11)  # do not send DEBUG
            logger.handlers.append(logstash_handler)
            logger.info('LogstashHandler is inited because you are in {} mode.'.format(__app.config['CONFIG_NAME']))

        # Sentry
        if __app.config['CONFIG_NAME'] in __app.config['SENTRY_AVAILABLE_IN']:
            sentry.init_app(app=__app)
            sentry_handler = SentryHandler(sentry.client, level='WARNING')  # Sentry 只处理 WARNING 以上的
            logger.handlers.append(sentry_handler)
            logger.info('Sentry is inited because you are in {} mode.'.format(__app.config['CONFIG_NAME']))

        # print current configuration
        import uwsgi
        if uwsgi.worker_id() == 1 and __first_spawn:
            # set to warning level because we want to monitor restarts
            logger.warning('App (re)started in `{0}` environment'
                           .format(__app.config['CONFIG_NAME']), stack=False)
            print_config(__app, logger)
            __first_spawn = False

    @uwsgidecorators.postfork
    def init_db():
        """init database connection"""
        import everyclass.server.db.mysql
        import everyclass.server.db.mongodb

        global __app
        everyclass.server.db.mysql.init_pool(__app)
        everyclass.server.db.mongodb.init_pool(__app)

    @uwsgidecorators.postfork
    def init_session():
        """init server-side session"""
        global __app
        __app.config['SESSION_MONGODB'] = __app.mongo
        Session(__app)

    @uwsgidecorators.postfork
    def init_recaptcha():
        """init reCaptcha"""
        ReCaptcha.VERIFY_URL = "https://www.recaptcha.net/recaptcha/api/siteverify"
        ReCaptcha.get_code = monkey_patch.ReCaptcha.get_code
        recaptcha.init_app(__app)

    @uwsgidecorators.postfork
    def get_android_download_link():
        """
        It's not possible to make a HTTP request during `create_app` since the urllib2 is patched by gevent
        and the gevent engine is not started yet (controlled by uWSGI). So we can only do the initialization
        here.
        """
        from everyclass.server.utils.rpc import HttpRpc
        android_manifest = HttpRpc.call(method="GET",
                                        url="https://everyclass.cdn.admirable.pro/android/manifest.json",
                                        retry=True)
        android_ver = android_manifest['latestVersions']['mainstream']['versionCode']
        __app.config['ANDROID_CLIENT_URL'] = android_manifest['releases'][android_ver]['url']
except ModuleNotFoundError:
    pass


def create_app() -> Flask:
    """创建 flask app"""
    from everyclass.server.db.dao import new_user_id_sequence
    from everyclass.server.utils.logbook_logstash.formatter import LOG_FORMAT_STRING
    from everyclass.server.consts import MSG_INTERNAL_ERROR
    from everyclass.server.utils import plugin_available

    print("Creating app...")

    app = Flask(__name__,
                static_folder='../../frontend/dist',
                static_url_path='',
                template_folder="../../frontend/templates")

    # load app config
    from everyclass.server.config import get_config
    _config = get_config()
    app.config.from_object(_config)  # noqa: T484

    """
    每课统一日志机制


    规则如下：
    - WARNING 以下 log 输出到 stdout
    - WARNING 以上输出到 stderr
    - DEBUG 以上日志以 json 形式通过 TCP 输出到 Logstash，然后发送到日志中心
    - WARNING 以上级别的输出到 Sentry


    日志等级：
    critical – for errors that lead to termination
    error – for errors that occur, but are handled
    warning – for exceptional circumstances that might not be errors
    notice – for non-error messages you usually want to see
    info – for messages you usually don’t want to see
    debug – for debug messages


    Sentry：
    https://docs.sentry.io/clients/python/api/#raven.Client.captureMessage
    - stack 默认是 False
    """
    if app.config['CONFIG_NAME'] in app.config['DEBUG_LOG_AVAILABLE_IN']:
        stdout_handler = logbook.StreamHandler(stream=sys.stdout, bubble=True, filter=lambda r, h: r.level < 13)
    else:
        # ignore debug when not in debug
        stdout_handler = logbook.StreamHandler(stream=sys.stdout, bubble=True, filter=lambda r, h: 10 < r.level < 13)
    stdout_handler.format_string = LOG_FORMAT_STRING
    logger.handlers.append(stdout_handler)

    stderr_handler = logbook.StreamHandler(stream=sys.stderr, bubble=True, level='WARNING')
    stderr_handler.format_string = LOG_FORMAT_STRING
    logger.handlers.append(stderr_handler)

    # CDN
    CDN(app)

    # 导入并注册 blueprints
    from everyclass.server.calendar.views import cal_blueprint
    from everyclass.server.query import query_blueprint
    from everyclass.server.views import main_blueprint as main_blueprint
    from everyclass.server.user.views import user_bp
    app.register_blueprint(cal_blueprint)
    app.register_blueprint(query_blueprint)
    app.register_blueprint(main_blueprint)

    # user feature gating
    if app.config['FEATURE_GATING']['user']:
        app.register_blueprint(user_bp, url_prefix='/user')

    @app.before_request
    def set_user_id():
        """在请求之前设置 session uid，方便 Elastic APM 记录用户请求"""
        if not session.get('user_id', None) and request.endpoint != "main.health_check":
            session['user_id'] = new_user_id_sequence()

    @app.after_request
    def response_minify(response):
        """用 htmlmin 压缩 HTML，减轻带宽压力"""
        if app.config['HTML_MINIFY'] and response.content_type == u'text/html; charset=utf-8':
            response.set_data(minify(response.get_data(as_text=True)))
        return response

    @app.template_filter('versioned')
    def version_filter(filename):
        """
        模板过滤器。如果 STATIC_VERSIONED，返回类似 'style-v1-c012dr.css' 的文件，而不是 'style-v1.css'

        :param filename: 文件名
        :return: 新的文件名
        """
        if app.config['STATIC_VERSIONED']:
            if filename[:4] == 'css/':
                new_filename = app.config['STATIC_MANIFEST'][filename[4:]]
                return 'css/' + new_filename
            elif filename[:3] == 'js/':
                new_filename = app.config['STATIC_MANIFEST'][filename[3:]]
                return new_filename
            else:
                return app.config['STATIC_MANIFEST'][filename]
        return filename

    @app.errorhandler(500)
    def internal_server_error(error):
        if plugin_available("sentry"):
            return render_template('common/error.html',
                                   message=MSG_INTERNAL_ERROR,
                                   event_id=g.sentry_event_id,
                                   public_dsn=sentry.client.get_public_dsn('https'))
        return "<h4>500 Error: {}</h4><br>You are seeing this page because Sentry is not available.".format(repr(error))

    global __app
    __app = app

    return app
