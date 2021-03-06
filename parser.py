import configparser
import sys
import time
import urllib.error
from os.path import getsize
from re import MULTILINE, finditer, sub

from bs4 import BeautifulSoup
from loguru import logger as log
from mutagen import File, id3
from mutagen.easyid3 import EasyID3
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto)
from vk_api import exceptions
from vk_api.audio import VkAudio, scrap_data
from wget import download


def get_data(group, api_vk):
    """
    Функция получения новых постов с серверов VK. В случае успеха возвращает словарь с постами, а в случае неудачи -
    ничего

    :param api_vk: Экземпляр класса VkApiMethod
    :param group: ID группы ВК
    :return: Возвращает список словарей с постами
    """
    # noinspection PyBroadException
    try:
        if group.startswith('club') or group.startswith('public') or '-' in group:
            group = group.replace('club', '-').replace('public', '-')
            feed = api_vk.wall.get(owner_id=group, count=11)
        else:
            feed = api_vk.wall.get(domain=group, count=11)
        return feed['items']
    except Exception:
        log.exception('Ошибка получения информации о новых постах: {0}'.format(sys.exc_info()[0]))
        return list()


def get_posts(domain, last_id, pinned_id, api_vk, config, session):
    log.info('[VK] Проверка на наличие новых постов в {0} с последним ID {1}'.format(domain, last_id))
    posts = get_data(domain, api_vk)
    send_reposts = config.get(domain, 'send_reposts', fallback=config.get('global', 'send_reposts'))
    for post in reversed(posts):
        is_pinned = post.get('is_pinned', False)
        if post['id'] > last_id or (is_pinned and post['id'] != pinned_id):
            log.info("[VK] Обнаружен новый пост с ID {0}".format(post['id']))
            parsed_post = VkPostParser(post, domain, session, api_vk, config)
            parsed_post.generate_post()
            if 'copy_history' in parsed_post.post:
                if send_reposts in ('no', 0):
                    log.info('Отправка репостов полностью отключена, поэтому пост будет пропущен.')
                elif send_reposts in ('post_only', 1):
                    yield parsed_post
                elif send_reposts in ('yes', 'all', 2):
                    yield parsed_post
                    parsed_post.generate_repost()
                    yield parsed_post.repost
            else:
                yield parsed_post
            if is_pinned:
                config.set(domain, 'pinned_id', str(post['id']))
            if post['id'] > last_id:
                config.set(domain, 'last_id', str(post['id']))
                last_id = post['id']
            time.sleep(5)
        elif post['id'] == last_id:
            log.info('[VK] Новых постов больше не обнаружено')


class VkPostParser:
    def __init__(self, post, group, session, api_vk, config, its_repost=False, what_to_parse=None):
        self.remixmdevice = '1920/1080/1/!!-!!!!'
        self.session = session
        self.audio_session = VkAudio(session)
        self.audio_session._vk.http.cookies.update(dict(remixmdevice=self.remixmdevice))
        self.api_vk = api_vk
        self.config = config
        self.pattern = '@' + group
        self.group = group
        self.post = post
        self.post_url = 'https://m.vk.com/wall{owner_id}_{id}'.format(**self.post)
        self.text = ''
        self.user = None
        self.repost = None
        self.repost_source = None
        self.reply_markup = None
        self.photos = []
        self.videos = []
        self.docs = []
        self.tracks = []
        self.attachments_types = set()
        self.its_repost = its_repost
        self.what_to_parse = what_to_parse

    def generate_post(self):
        log.info('[AP] Парсинг поста...')
        if not self.its_repost:
            try:
                self.what_to_parse = self.config.get(self.group, 'what_to_send').split(',')
            except (configparser.NoOptionError, configparser.NoSectionError):
                self.what_to_parse = self.config.get('global', 'what_to_send', fallback='all').split(',')
        if self.config.getboolean('global', 'sign_posts'):
            self.generate_user()
        if 'attachments' in self.post:
            for attachment in self.post['attachments']:
                self.attachments_types.add(attachment['type'])
        if set(self.what_to_parse).intersection({'link', 'text', 'all'}):
            self.generate_text()
            self.generate_links()
        if self.config.getboolean('global', 'sign_posts'):
            self.sign_post()
        if set(self.what_to_parse).intersection({'photo', 'all'}):
            self.generate_photos()
        if set(self.what_to_parse).intersection({'video', 'all'}):
            self.generate_videos()
        if set(self.what_to_parse).intersection({'doc', 'all'}):
            self.generate_docs()
        if set(self.what_to_parse).intersection({'music', 'all'}):
            self.generate_music()

    def generate_text(self):
        if self.post['text']:
            log.info('[AP] Обнаружен текст. Извлечение...')
            self.text += self.post['text'] + '\n'
            if self.pattern != '@':
                self.text = self.text.replace(self.pattern, '')
            # self.generate_links()
            matches = finditer(r'\[(.*?)\]', self.text, MULTILINE)
            result = {}
            for _, match in enumerate(matches):
                for group_num in range(0, len(match.groups())):
                    group_num = group_num + 1
                    result[match.group()] = match.group(group_num)
            try:
                for i in result.keys():
                    self.text = self.text.replace(i, '<a href="https://vk.com/{}">{}</a>'.format(*result[i].split('|')))
            except IndexError:
                pass

    def generate_links(self):
        if 'attachments' in self.post:
            for attachment in self.post['attachments']:
                if attachment['type'] == 'link' and attachment['link']['title']:
                    self.text += '\n🔗 <a href="{url}">{title}</a>'.format(**attachment['link'])
                elif attachment['type'] == 'page':
                    self.text += '\n🔗 <a href="{view_url}">{title}</a>'.format(**attachment['page'])
                elif attachment['type'] == 'album':
                    self.text += '\n<a href="https://vk.com/wall{owner_id}_{id}">' \
                                 'Фото альбом: {title}</a>'.format(**attachment['album'])

    def generate_photos(self):
        if 'photo' in self.attachments_types:
            photo = None
            counter = 1
            log.info('[AP] Извлечение фото...')
            for attachment in self.post['attachments']:
                if attachment['type'] == 'photo':
                    for i in attachment['photo']['sizes']:
                        photo = i['url']
                    if photo and counter == 1:
                        if len(self.text) < 1024:
                            self.photos.append(InputMediaPhoto(photo, caption=self.text, parse_mode='HTML'))
                        else:
                            self.photos.append(InputMediaPhoto(photo))
                    elif photo:
                        self.photos.append(InputMediaPhoto(photo))
                    counter += 1

    def generate_docs(self):
        if 'doc' in self.attachments_types:
            log.info('[AP] Извлечение вложениий (файлы, гифки и т.п.)...')
            for attachment in self.post['attachments']:
                if attachment['type'] == 'doc' and attachment['doc']['size'] < 52428800:
                    try:
                        doc = download(attachment['doc']['url'], out='file.{ext}'.format(**attachment['doc']))
                        self.docs.append([doc, '{title}.{ext}'.format(**attachment['doc'])])
                    except urllib.error.URLError:
                        log.exception('[AP] Невозможно скачать вложенный файл: {0}.'.format(sys.exc_info()[1]))
                        self.text += '\n📃 <a href="{url}">{title}</a>'.format(**attachment['doc'])
                elif attachment['type'] == 'doc' and attachment['doc']['size'] >= 52428800:
                    self.text += '\n📃 <a href="{url}">{title}</a>'.format(**attachment['doc'])

    def generate_videos(self):
        if 'video' in self.attachments_types:
            log.info('[AP] Извлечение видео...')
            for attachment in self.post['attachments']:
                if attachment['type'] == 'video':
                    video = 'https://m.vk.com/video{owner_id}_{id}'.format(**attachment['video'])
                    soup = BeautifulSoup(self.session.http.get(video).text, 'html.parser')
                    if soup.find_all('source'):
                        video_link = soup.find_all('source')[1].get('src')
                        file = download(video_link)
                        if getsize(file) > 52428800:
                            log.info('[AP] Видео весит более 50 МиБ. Добавляем ссылку на видео в текст.')
                            self.text += '\n🎥 <a href="{0}">{1[title]}</a>\n👁 {1[views]} раз(а)' \
                                         ' ⏳ {1[duration]} сек'.format(video, attachment['video'])
                            del file
                            continue
                        self.videos.append(file)
                    elif soup.iframe:
                        self.text += '\n🎥 <a href="{0}">{1[title]}</a>\n👁 {1[views]} раз(а) ⏳ {1[duration]} сек'.format(
                            video, attachment['video'])

    def generate_music(self):
        if 'audio' in self.attachments_types:
            log.info('[AP] Извлечение аудио...')
            user_id = self.api_vk.users.get()[0]['id']
            response = self.audio_session._vk.http.get(self.post_url)
            tracks = scrap_data(response.text, user_id, filter_root_el={'class': 'audios_list'})
            for track in tracks:
                name = sub(r"[^a-zA-Z '#0-9.а-яА-Я()-]", '',
                           track['artist'] + ' - ' + track['title'] + '.mp3')
                try:
                    file = download(track['url'], out=name)
                except (urllib.error.URLError, IndexError):
                    log.exception('[AP] Не удалось скачать аудиозапись. Пропускаем ее...')
                    continue
                if getsize(file) > 52428800:
                    log.warning('[AP] Файл весит более 50 МиБ. Пропускаем его...')
                    continue
                try:
                    music = EasyID3(file)
                except id3.ID3NoHeaderError:
                    music = File(file, easy=True)
                    music.add_tags()
                music['title'] = track['title']
                music['artist'] = track['artist']
                music.save()
                del music
                self.tracks.append((name, track['duration']))

    def sign_post(self):
        photos = 0
        if 'photo' in self.attachments_types:
            for attachment in self.post['attachments']:
                if attachment['type'] == 'photo':
                    photos += 1
        post_url = self.post_url.replace('m.', '')
        button_list = []
        if self.user:
            log.info('[AP] Подписывание поста и добавление ссылки на его оригинал.')
            user = 'https://vk.com/{0[domain]}'.format(self.user)
            button_list.append(InlineKeyboardButton('Автор поста: {first_name} {last_name}'.format(**self.user), url=user))
            self.reply_markup = InlineKeyboardMarkup(build_menu(button_list, n_cols=1))
            if photos > 1:
                self.text += '\nАвтор поста: <a href="{}">{first_name} {last_name}</a>'.format(user, **self.user)
                self.text += '\n<a href="{}">Оригинал поста</a>'.format(post_url)
            else:
                button_list.append(InlineKeyboardButton('Оригинал поста', url=post_url))
        else:
            if photos > 1:
                self.text += '\n<a href="{}">Оригинал поста</a>'.format(post_url)
            else:
                button_list.append(InlineKeyboardButton('Оригинал поста', url=post_url))
            log.info('[AP] Добавление только ссылки на оригинал поста, так как в нем не указан автор.')
            self.reply_markup = InlineKeyboardMarkup(build_menu(button_list, n_cols=2))

    def generate_user(self):
        if 'signer_id' in self.post:
            self.user = self.api_vk.users.get(user_ids=self.post['signer_id'], fields='domain')[0]

    def generate_repost(self):
        log.info('Включена отправка репоста. Начинаем парсинг репоста.')
        source_id = int(self.post['copy_history'][0]['from_id'])
        try:
            source_info = self.api_vk.groups.getById(group_id=-source_id)[0]
            repost_source = 'Репост из <a href="https://vk.com/{screen_name}">{name}</a>:\n\n'.format(**source_info)
        except exceptions.ApiError:
            source_info = self.api_vk.users.get(user_ids=source_id)[0]
            repost_source = 'Репост от <a href="https://vk.com/id{id}">' \
                            '{first_name} {last_name}</a>:\n\n'.format(**source_info)
        self.repost = VkPostParser(self.post['copy_history'][0], source_info.get('screen_name', ''), self.session,
                                   self.api_vk, self.config, True, self.what_to_parse)
        self.repost.text = repost_source
        self.repost.generate_post()


def build_menu(buttons, n_cols, header_buttons=None, footer_buttons=None):
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)
    return menu
