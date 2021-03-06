import json
import random
import re
import time
from bs4 import BeautifulSoup
from datetime import datetime

import requests

from utils import pgs, es

wx_mps = 'wxmps'  # 这里数据库、用户、密码一致(需替换成实际的)
postgres = pgs.Pgs(host='localhost', port='12432', db_name=wx_mps, user=wx_mps, password=wx_mps)
elastic = es.Es(host='localhost', port=12900, index='mp', doc='article')


def load():
    for i in range(1, 19):
        sql = "select id,mps_biz,last_msg_id,app_msg_token,pass_ticket,wap_sid2 from tb_mps where id = {}".format(i)
        result = postgres.fetch_all(sql)
        for r in result:
            r_id, r_mps_biz, r_last_msg_id, r_app_msg_token, r_pass_ticket, r_wap_sid2 = r[0], r[1], r[2], \
                                                                                         r[3], r[4], r[5]
            r_cookie = 'wxuin=1604513290; version=62060426; pass_ticket={}; wap_sid2={}' \
                .format(r_pass_ticket, r_wap_sid2)
            mps = WxMps(r_id, r_mps_biz, r_pass_ticket, r_app_msg_token, r_cookie, r_last_msg_id)
            mps.start()


class WxMps(object):
    """微信公众号文章、评论抓取爬虫"""

    def __init__(self, _mps_id, _biz, _pass_ticket, _app_msg_token, _cookie, last_msg_id=0, _offset=0):
        self.offset = _offset
        self.mps_id = _mps_id
        self.last_msg_id = last_msg_id  # 上次抓取的最后消息的id
        self.biz = _biz  # 公众号标志
        self.msg_token = _app_msg_token  # 票据(非固定)
        self.pass_ticket = _pass_ticket  # 票据(非固定)
        self.headers = {
            'Cookie': _cookie,  # Cookie(非固定)
            'User-Agent': 'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/57.0.2987.132 '
        }

    def start(self):
        """请求获取公众号的文章接口"""

        start = True
        offset = self.offset
        while start:
            api = 'https://mp.weixin.qq.com/mp/profile_ext?action=getmsg&__biz={0}&f=json&offset={1}' \
                  '&count=10&is_ok=1&scene=124&uin=777&key=777&pass_ticket={2}&wxtoken=&appmsg_token' \
                  '={3}&x5=1&f=json'.format(self.biz, offset, self.pass_ticket, self.msg_token)

            resp = requests.get(api, headers=self.headers, verify=False).json()
            ret, status = resp.get('ret'), resp.get('errmsg')  # 状态信息
            if 0 == ret or 'ok' == status:
                # print('Crawl article: ' + api)

                general_msg_list = resp['general_msg_list']
                msg_list = json.loads(general_msg_list)['list']  # 获取文章列表
                for msg in msg_list:
                    comm_msg_info = msg['comm_msg_info']  # 该数据是本次推送多篇文章公共的
                    msg_id = comm_msg_info['id']  # 文章id
                    if msg_id == self.last_msg_id:
                        start = False
                        break
                    post_time = datetime.fromtimestamp(comm_msg_info['datetime'])  # 发布时间
                    msg_type = comm_msg_info['type']  # 文章类型
                    # msg_data = json.dumps(comm_msg_info, ensure_ascii=False)  # msg原数据

                    if 49 == msg_type:
                        # 图文消息
                        app_msg_ext_info = msg.get('app_msg_ext_info')  # article原数据
                        if app_msg_ext_info:
                            # 本次推送的首条文章
                            self._parse_articles(app_msg_ext_info, msg_id, post_time, msg_type)
                            # 本次推送的其余文章
                            multi_app_msg_item_list = app_msg_ext_info.get('multi_app_msg_item_list')
                            if multi_app_msg_item_list:
                                for item in multi_app_msg_item_list:
                                    msg_id = item['fileid']  # 文章id
                                    if msg_id or not isinstance(msg_id, int):
                                        msg_id = int(time.time())  # 设置唯一id,解决部分文章id=0出现唯一索引冲突的情况
                                    self._parse_articles(item, msg_id, post_time, msg_type)
                    elif 1 == msg_type:
                        # 文字消息
                        content = comm_msg_info.get('content')
                        if content:
                            self._save_text_and_image(msg_id, post_time, msg_type, digest=content)
                    elif 3 == msg_type:
                        # 图片消息
                        image_msg_ext_info = msg.get('image_msg_ext_info')
                        cdn_url = image_msg_ext_info.get('cdn_url')
                        if cdn_url:
                            self._save_text_and_image(msg_id, post_time, msg_type, cover=cdn_url)

            # 0：结束；1：继续
            can_msg_continue = resp.get('can_msg_continue')
            if not can_msg_continue:
                print('Break , Current offset : %d' % offset)
                break
            offset = resp.get('next_offset')  # 下一次请求偏移量
            print('Next offset : %d' % offset)

    def _save_es(self, json_data, article_id):
        elastic.put_data(data_body=json_data, _id=article_id)

    def _save_text_and_image(self, msg_id, post_time, msg_type, cover=None, digest=None):
        """保存只是文字或图片消息"""

        article_id = postgres.handler(self._save_only_article(), (
            msg_id, cover, digest, post_time, datetime.now(), self.mps_id, msg_type), fetch=True)

        if article_id:
            json_data = {"articleId": article_id, "cover": cover, "digest": digest, "mpsId": self.mps_id,
                         "msgId": msg_id, "postTime": post_time, "msgType": msg_type}
            self._save_es(json_data, article_id)

    @staticmethod
    def crawl_article_content(content_url):
        """抓取文章内容
        :param content_url: 文章地址
        """

        try:
            html = requests.get(content_url, verify=False).text
        except:
            print(content_url)
            pass
        else:
            bs = BeautifulSoup(html, 'html.parser')
            js_content = bs.find(id='js_content')
            if js_content:
                p_list = js_content.find_all('p')
                content_list = list(map(lambda p: p.text, filter(lambda p: p.text != '', p_list)))
                content = ''.join(content_list)
                return content

    def _parse_articles(self, info, msg_id, post_time, msg_type):
        """解析嵌套文章数据并保存入库"""

        title = info.get('title')  # 标题
        cover = info.get('cover')  # 封面图
        author = info.get('author')  # 作者
        digest = info.get('digest')  # 关键字
        source_url = info.get('source_url')  # 原文地址
        content_url = info.get('content_url')  # 微信地址
        # ext_data = json.dumps(info, ensure_ascii=False)  # 原始数据

        content_url = content_url.replace('amp;', '').replace('#wechat_redirect', '').replace('http', 'https')
        content = self.crawl_article_content(content_url)
        article_id = postgres.handler(self._save_article(), (msg_id, title, author, cover, digest, source_url,
                                                             content_url, post_time, datetime.now(),
                                                             self.mps_id, content, msg_type), fetch=True)
        if article_id:
            json_data = {"articleId": article_id, "author": author, "content": content,
                         "contentURL": content_url, "cover": cover, "digest": digest,
                         "mpsId": self.mps_id, "msgId": msg_id, "postTime": post_time,
                         "sourceURL": source_url, "title": title, "msgType": msg_type}
            self._save_es(json_data, article_id)
            time.sleep(random.randint(2, 3))
            self._parse_article_detail(content_url, article_id)

    def _parse_article_detail(self, content_url, article_id):
        """从文章页提取相关参数用于获取评论,article_id是已保存的文章id"""

        try:
            resp = requests.get(content_url, headers=self.headers, verify=False)
        except Exception as e:
            print('获取评论失败 {} {}'.format(article_id, content_url))
        else:
            # group(0) is current line
            html = resp.text
            str_comment = re.search(r'var comment_id = "(.*)" \|\| "(.*)" \* 1;', html)
            str_msg = re.search(r"var appmsgid = '' \|\| '(.*)'\|\|", html)
            str_token = re.search(r'window.appmsg_token = "(.*)";', html)

            if str_comment and str_msg and str_token:
                comment_id = str_comment.group(1)  # 评论id(固定)
                app_msg_id = str_msg.group(1)  # 票据id(非固定)
                appmsg_token = str_token.group(1)  # 票据token(非固定)

                # 缺一不可
                if appmsg_token and app_msg_id and comment_id:
                    print('Crawl article {} comments'.format(article_id))
                    self._crawl_comments(app_msg_id, comment_id, appmsg_token, article_id)

    def _crawl_comments(self, app_msg_id, comment_id, appmsg_token, article_id):
        """抓取文章的评论"""

        api = 'https://mp.weixin.qq.com/mp/appmsg_comment?action=getcomment&scene=0&__biz={0}' \
              '&appmsgid={1}&idx=1&comment_id={2}&offset=0&limit=100&uin=777&key=777' \
              '&pass_ticket={3}&wxtoken=777&devicetype=android-26&clientversion=26060739' \
              '&appmsg_token={4}&x5=1&f=json'.format(self.biz, app_msg_id, comment_id,
                                                     self.pass_ticket, appmsg_token)
        try:
            resp = requests.get(api, headers=self.headers, verify=False).json()
        except:
            print('Article {} no Comment'.format(article_id))
        else:
            ret, status = resp['base_resp']['ret'], resp['base_resp']['errmsg']
            if ret == 0 or status == 'ok':
                elected_comment = resp['elected_comment']
                for comment in elected_comment:
                    nick_name = comment.get('nick_name')  # 昵称
                    logo_url = comment.get('logo_url')  # 头像
                    comment_time = datetime.fromtimestamp(comment.get('create_time'))  # 评论时间
                    content = comment.get('content')  # 评论内容
                    content_id = comment.get('content_id')  # id
                    like_num = comment.get('like_num')  # 点赞数

                    reply_list = comment.get('reply')['reply_list']  # 回复数据
                    reply_content, reply_like_num, reply_create_time = None, None, None
                    if reply_list:
                        first_reply = reply_list[0]
                        reply_content = first_reply.get('content')
                        reply_like_num = first_reply.get('reply_like_num')
                        reply_create_time = datetime.fromtimestamp(first_reply.get('create_time'))

                    postgres.handler(self._save_article_comment(), (article_id, comment_id, nick_name, logo_url,
                                                                    content_id, content, like_num, comment_time,
                                                                    datetime.now(), reply_content, reply_like_num,
                                                                    reply_create_time, self.mps_id))

    @staticmethod
    def _save_only_article():
        sql = 'insert into tb_article(msg_id,cover,digest,post_time,create_time,mps_id,msg_type) ' \
              'values(%s,%s,%s,%s,%s,%s,%s) returning id'
        return sql

    @staticmethod
    def _save_article():
        sql = 'insert into tb_article(msg_id,title,author,cover,digest,source_url,content_url,post_time,create_time,' \
              'mps_id,content,msg_type) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id'
        return sql

    @staticmethod
    def _save_article_comment():
        sql = 'insert into tb_article_comment(article_id,comment_id,nick_name,logo_url,content_id,content,like_num,' \
              'comment_time,create_time,reply_content,reply_like_num,reply_create_time,mps_id) ' \
              'values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
        return sql


if __name__ == '__main__':
    # _id = 18
    # biz = 'MjM5NjAxOTU4MA=='
    # app_msg_token = '987_cjLuYj%2BqEZfnkrXFuo06pvB9JHXZHHgSqtFVjg~~'
    # # 上面3个定义与公众号相关【_id仅为个人数据库中的一个标志】，下面3个跟当前微信会话有关
    # pass_ticket = '4UNJTJN77aahj26LxFmo3IgBRs2kAqTpJn++Y/0stu3Q+nXVF8zb00nOMkRiFq5h'
    # wap_sid2 = 'CIrci/0FElx1T2p1M2d6NFczNk1Dd05NUHRab0Fjb205ekxNSlBHV3VlNTVOYXl0Qk5ZUDc3Y2JsZ01jclczYWx3eXdMNUNoMDZhOUF4SkUxRHNIRHc3aG1SZExRZHNEQUFBfjDFjcfgBTgNQJVO'
    # cookie = 'wxuin=1604513290; version=62060426; pass_ticket={}; wap_sid2={}'.format(pass_ticket, wap_sid2)
    # wxMps = WxMps(_id, biz, pass_ticket, app_msg_token, cookie)
    # # 开始爬取文章及评论
    # wxMps.start()
    load()
