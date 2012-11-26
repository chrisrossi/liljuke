#!/usr/bin/python
import json
import os
import sys
import time

from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3


class LilJuke(object):

    def __init__(self, folder):
        self.folder = folder
        self.dbfile = dbfile = os.path.join(folder, '.liljuke.db')
        if os.path.exists(dbfile):
            self.albums = [Album.from_json(album) for album in
                           json.load(open(dbfile, 'rb'))]
        else:
            self.albums = []

        self.scan_albums(folder, self.albums)
        self.save()

    def save(self):
        data = [album.as_json() for album in self.albums]
        with open(self.dbfile, 'wb') as f:
            json.dump(data, f, indent=4)

    def scan_albums(self, folder, albums):
        visited = set([album.path for album in albums])
        def visit(path):
            if path in visited:
                return
            files = os.listdir(path)
            album = None
            for fname in files:
                ext = os.path.splitext(fname.lower())[1]
                if ext in ('.flac', '.ogg', '.mp3'):
                    album = Album()
                    break

            if album:
                cover = None
                for fname in files:
                    fpath = os.path.join(path, fname)
                    ext = os.path.splitext(fname.lower())[1]

                    if ext in MUSIC_EXTS:
                        track_data = get_track_data(fpath, fname, ext)
                        if not track_data:
                            return
                        discnum, tracknum = track_data
                        album.tracks.append(Track(fpath, discnum, tracknum))

                    elif not cover and ext in COVER_EXTS:
                        cover = fpath

                album.tracks.sort(key=Track.sort_key)

                if not cover:
                    cover = extract_cover(album.tracks[0].path)

                if not cover:
                    print "Skipping %s, no album cover found" % path
                    return

                album.added = os.path.getmtime(album.tracks[0].path)
                album.plays = 1
                album.path = path
                album.cover = cover
                albums.append(album)
                print 'Added', album.path
                self.save()

            else:
                for fname in files:
                    child = os.path.join(path, fname)
                    if os.path.isdir(child):
                        visit(child)
                    else:
                        print "WTF?", child

        visit(folder)
        albums.sort(key=Album.sort_key, reverse=True)


class Album(object):
    properties = ('added', 'plays', 'path', 'cover')

    def __init__(self):
        self.tracks = []

    def as_json(self):
        data = {prop: getattr(self, prop) for prop in self.properties}
        data['tracks'] = [track.as_json() for track in self.tracks]
        return data

    @classmethod
    def from_json(cls, data):
        album = cls.__new__(cls)
        for prop in album.properties:
            setattr(album, prop, data[prop])
        album.tracks = [Track.from_json(track) for track in data['tracks']]
        return album

    def is_recent(self):
        thirty_days_ago = time.time() - 30 * 24 * 3600
        return self.added > thirty_days_ago

    def sort_key(self):
        return self.is_recent(), self.plays, self.added


class Track(object):
    properties = ('discnum', 'tracknum', 'path')

    def __init__(self, path, discnum, tracknum):
        self.path = path
        self.discnum = discnum
        self.tracknum = tracknum

    def as_json(self):
        return {prop: getattr(self, prop) for prop in self.properties}

    @classmethod
    def from_json(cls, data):
        track = cls.__new__(cls)
        for prop in track.properties:
            setattr(track, prop, data[prop])
        return track

    def sort_key(self):
        return self.discnum, self.tracknum, self.path



def get_track_data(path, fname, ext):
    tags = CODECS[ext](path)
    discnum = number(tags.get('discnumber', '1'))
    tracknum = tags.get('tracknumber')
    if not tracknum:
        i = 0
        while fname[i].isdigit():
            i += 1
        if not i:
            print "Unable to find track number for %s", path
            return None
        tracknum = fname[:i]
    tracknum = number(tracknum)
    return discnum, tracknum


def extract_cover(path):
    ext = os.path.splitext(path.lower())[1]
    cover = None
    if ext == '.mp3':
        tags = ID3(path)
        cover = tags.get('APIC:')
    else:
        tags = CODECS[ext](path)
        if hasattr(tags, 'pictures') and tags.pictures:
            cover = tags.pictures[0]
    if cover:
        assert cover.mime in IMAGE_TYPES, (cover.mime, path)
        if hasattr(cover, 'encoding'):
            assert cover.encoding == 0, path
        folder = os.path.dirname(path)
        cover_path = os.path.join(folder, 'cover' + IMAGE_TYPES[cover.mime])
        with open(cover_path, 'wb') as f:
            f.write(cover.data)
        return cover_path


def number(s):
    """
    Yes, mutagen is this annoying.
    """
    if isinstance(s, list):
        s = s[0]
    if '/' in s:
        s, total = s.split('/')
    return int(s)


MUSIC_EXTS = ('.flac', '.ogg', '.mp3')
CODECS = {
    '.flac': FLAC,
    '.mp3': EasyID3,
    '.ogg': OggVorbis
}
COVER_EXTS = ('.gif', '.png', '.jpg', '.jpeg', '.bmp')
IMAGE_TYPES = {
    'image/gif': '.gif',
    'image/png': '.png',
    'image/jpg': '.jpg',
    'image/jpeg': '.jpg'}


if __name__ == '__main__':
    folder = sys.argv[1]
    assert os.path.isdir(folder)
    LilJuke(os.path.abspath(folder))
