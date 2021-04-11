import logging
import os

from fuocore.media import Quality, Media
from fuocore.models import cached_field
from fuocore.models import (
    BaseModel,
    SongModel,
    LyricModel,
    PlaylistModel,
    AlbumModel,
    ArtistModel,
    SearchModel,
    MvModel,
    UserModel,
    ModelStage,
    SearchType,
)

from fuocore.reader import SequentialReader, wrap as reader_wrap

from .provider import provider

logger = logging.getLogger(__name__)


def _deserialize(data, schema_cls, gotten=True):
    schema = schema_cls()
    obj = schema.load(data)
    # XXX: 将 model 设置为 gotten，减少代码编写时的心智负担，
    # 避免在调用 get 方法时进入无限递归。
    if gotten:
        obj.stage = ModelStage.gotten
    return obj


def create_g(func, identifier, schema):
    data = func(identifier, page=1)
    total = int(data['total'])

    def g():
        nonlocal data
        if data is None:
            yield from ()
        else:
            page = 1
            while data['list']:
                obj_data_list = data['list']
                for obj_data in obj_data_list:
                    obj = _deserialize(obj_data, schema, gotten=False)
                    # FIXME: 由于 feeluown 展示歌手的 album 列表时，
                    # 会依次同步的去获取 cover，所以我们这里必须先把 cover 初始化好，
                    # 否则 feeluown 界面会卡住
                    if schema == _ArtistAlbumSchema:
                        obj.cover = provider.api.get_cover(obj.mid, 2)
                    yield obj
                page += 1
                data = func(identifier, page)

    return SequentialReader(g(), total)


class QQBaseModel(BaseModel):
    _api = provider.api

    class Meta:
        allow_get = True
        provider = provider
        fields = ('mid', )

    @classmethod
    def get(cls, identifier):

        raise NotImplementedError


class QQMvModel(MvModel, QQBaseModel):
    class Meta:
        fields = ['q_url_mapping']
        support_multi_quality = True

    @classmethod
    def get(cls, identifier):
        data = cls._api.get_mv(identifier)
        if not data:
            return None
        fhd = hd = sd = ld = None
        for file in data['mp4']:
            if not file['url']:
                continue
            file_type = file['filetype']
            url = file['freeflow_url'][0]
            if file_type == 40:
                fhd = url
            elif file_type == 30:
                hd = url
            elif file_type == 20:
                sd = url
            elif file_type == 10:
                ld = url
            elif file_type == 0:
                pass
            else:
                logger.warning('There exists another quality:%s mv.', str(file_type))
        q_url_mapping = dict(fhd=fhd, hd=hd, sd=sd, ld=ld)
        return QQMvModel(identifier=identifier,
                         q_url_mapping=q_url_mapping)

    def list_quality(self):
        return list(key for key, value in self.q_url_mapping.items()
                    if value is not None)

    def get_media(self, quality):
        if isinstance(quality, Quality.Video):  # Quality.Video Enum Item
            quality = quality.value
        return self.q_url_mapping.get(quality)


class QQLyricModel(LyricModel, QQBaseModel):
    @classmethod
    def get(cls, identifier):
        content = cls._api.get_lyric_by_songmid(identifier)
        return cls(identifier=identifier, content=content)


class QQSongModel(SongModel, QQBaseModel):

    class Meta:
        fields = ('mid', 'media_id', 'mvid', 'q_media_mapping', 'quality_suffix')
        fields_no_get = ('mv', 'lyric', 'q_media_mapping')
        support_multi_quality = True

    @classmethod
    def get(cls, identifier):
        data = cls._api.get_song_detail(identifier)
        song = _deserialize(data, QQSongSchema)
        return song

    @cached_field()
    def lyric(self):
        return QQLyricModel.get(self.mid)

    @cached_field(ttl=100)
    def mv(self):
        if self.mvid is None:
            return None
        return QQMvModel.get(self.mvid)

    @cached_field(ttl=1000)
    def q_media_mapping(self):
        """fetch media info and save it in q_media_mapping"""
        use_v2 = False
        try:
            import execjs
        except ImportError:
            pass
        else:
            # 当 execjs 库安装的时候，默认使用 v2 接口，
            # 用户可以通过设置环境变量来主动关闭（可以在 fuorc 文件中设置）。
            if 'FUO_QQMUSIC_JSENGINE_DISABLE' not in os.environ:
                use_v2 = True

        q_media_mapping = {}
        if use_v2 is True:
            # 注：self.quality_suffix 这里可能会触发一次网络请求
            for idx, (q, t, b, s) in enumerate(self.quality_suffix):
                url = self._api.get_song_url_v2(self.mid, self.media_id, t)
                if url:
                    q_media_mapping[q] = Media(url, bitrate=b, format=s)
        else:
            q_urls_mapping = self._api.get_song_url(self.mid)
            q_bitrate_mapping = {'shq': 1000,
                                 'hq': 800,
                                 'sq': 500,
                                 'lq': 64}
            q_media_mapping = {}
            for quality, url in q_urls_mapping.items():
                bitrate = q_bitrate_mapping[quality]
                format = url.split('?')[0].split('.')[-1]
                q_media_mapping[quality] = Media(url, bitrate=bitrate, format=format)

        self.q_media_mapping = q_media_mapping
        return q_media_mapping

    @cached_field(ttl=600)
    def url(self):
        medias = list(self.q_media_mapping.values())
        if medias:
            return medias[0].url
        return ''

    def list_quality(self):
        return list(self.q_media_mapping.keys())

    def get_media(self, quality):
        return self.q_media_mapping.get(quality)


class QQAlbumModel(AlbumModel, QQBaseModel):
    class Meta:
        fields = ['mid']

    @classmethod
    def get(cls, identifier):
        data_album = cls._api.album_detail(identifier)
        if data_album is None:
            return None
        album = _deserialize(data_album, QQAlbumSchema)
        album.cover = cls._api.get_cover(album.mid, 2)
        return album

    # def _more_info(self):
    #     """this function will get more info such as genre, date, track & disc (just for tag completion)"""
    #     data = self._api.album_detail(self.identifier)
    #     if data is None:
    #         return {}
    #
    #     # 有时候显示的歌手名有问题，需要专门请求(如fuo://qqmusic/songs/217490728包含了好几个歌手)
    #     if '/' in self.artists[0].name:
    #         self.artists[0].name = self._api.artist_detail(self.artists[0].identifier)['singer_name']
    #     import re
    #     fil = re.compile(u'[^0-9a-zA-Z/&]+', re.UNICODE)
    #     tag_info = {
    #         'albumartist': self.artists_name,
    #         'date': data['getAlbumInfo']['Fpublic_time'] + 'T00:00:00',
    #         'genre': (fil.sub(' ', data['genre'])).strip()}
    #
    #     try:
    #         songs_identifier = [int(song['id']) for song in data['getSongInfo']]
    #         songs_disc = [song['index_cd'] + 1 for song in data['getSongInfo']]
    #         disc_counts = {x: songs_disc.count(x) for x in range(1, max(songs_disc) + 1)}
    #         track_bias = [0]
    #         for i in range(1, len(disc_counts)):
    #             track_bias.append(track_bias[-1] + disc_counts[i])
    #         tag_info['discs'] = dict(zip(songs_identifier, [str(disc) + '/' + str(songs_disc[-1])
    #                                                         for disc in songs_disc]))
    #         tag_info['tracks'] = dict(zip(songs_identifier, [
    #             str(song['index_album'] - track_bias[song['index_cd']]) + '/' + str(disc_counts[song['index_cd'] + 1])
    #             for song in data['getSongInfo']]))
    #     except Exception as e:
    #         logger.error(e)
    #     return tag_info


class QQArtistModel(ArtistModel, QQBaseModel):
    class Meta:
        allow_create_songs_g = True
        allow_create_albums_g = True

    @classmethod
    def get(cls, identifier):
        data_artist = cls._api.artist_detail(identifier)
        artist = _deserialize(data_artist, QQArtistSchema)
        artist.cover = cls._api.get_cover(artist.mid, 1)
        return artist

    def create_songs_g(self):
        return create_g(self._api.artist_detail,
                        self.identifier,
                        _ArtistSongSchema)

    def create_albums_g(self):
        return create_g(self._api.artist_albums,
                        self.identifier,
                        _ArtistAlbumSchema)


class QQPlaylistModel(PlaylistModel, QQBaseModel):
    class Meta:
        allow_create_songs_g = True

    @classmethod
    def get(cls, identifier):
        data = cls._api.get_playlist(identifier)
        return _deserialize(data, QQPlaylistSchema)

    def create_songs_g(self):
        # 歌单曲目数不能超过 1000，所以它可能不需要分页
        return reader_wrap(self.songs)


class QQSearchModel(SearchModel, QQBaseModel):
    pass


class QQUserModel(UserModel, QQBaseModel):
    class Meta:
        fields = ('cookies',)
        fields_no_get = ('cookies', 'rec_songs', 'rec_playlists',
                         'fav_artists', 'fav_albums', )

    @classmethod
    def get(cls, identifier):
        data = cls._api.get_user_info(identifier)
        return _deserialize(data, QQUserSchema)

    @cached_field(ttl=5)  # ttl should be 0
    def fav_albums(self):
        # TODO: fetch more if total count > 100
        albums = self._api.user_favorite_albums(self.identifier)
        return [_deserialize(album, QQAlbumSchema) for album in albums]

    @cached_field(ttl=5)  # ttl should be 0
    def rec_songs(self):
        pid = self._api.get_recommend_songs_pid()
        playlist = QQPlaylistModel.get(pid)
        return playlist.songs


def search(keyword, **kwargs):
    # TODO: support to search artist/album/playlist
    data_songs = provider.api.search(keyword)
    songs = []
    for data_song in data_songs:
        song = _deserialize(data_song, QQSongSchema)
        songs.append(song)
    return QQSearchModel(songs=songs)
    # type_ = SearchType.parse(kwargs['type_'])
    # if type_ == SearchType.pl:
    #     data = provider.api.search_playlist(keyword)
    # else:
    #     type_type_map = {
    #         SearchType.so: 0,
    #         SearchType.al: 8,
    #         SearchType.ar: 9,
    #     }
    #     data = provider.api.search(keyword, type_=type_type_map[type_])
    # result = _deserialize(data, QQSearchSchema)
    # result.q = keyword
    # return result


base_model = QQBaseModel()

from .schemas import (  # noqa
    QQPlaylistSchema,
    QQUserSchema,
    QQSongSchema,
    QQArtistSchema,
    QQAlbumSchema,
    _ArtistSongSchema,
    _ArtistAlbumSchema,
)
