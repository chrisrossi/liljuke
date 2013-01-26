#!/usr/bin/python
import json
import os
import subprocess
import sys
import time

import pygame

from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3

BACKGROUND = (0, 0, 0)


class LilJuke(object):
    IDLE = 0
    PLAYING = 1

    def __init__(self, folder):
        print "Initializing..."
        self.folder = folder
        self.dbfile = dbfile = os.path.join(folder, '.liljuke.db')
        if os.path.exists(dbfile):
            self.albums = [Album.from_json(album) for album in
                           json.load(open(dbfile, 'rb'))]
        else:
            self.albums = []

        self.scan_albums(folder, self.albums)
        self.save()

        self.state = self.IDLE

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
                    if os.path.exists(os.path.join(path, '.liljuke')):
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

        visit(folder)
        albums.sort(key=Album.sort_key, reverse=True)

    def run(self, fullscreen):
        print "Running..."
        pygame.init()
        subprocess.call(['mocp', '--server'])
        if fullscreen:
            self.screen = pygame.display.set_mode((0,0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode((520, 390))
        self.set_album(0)
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.KEYDOWN:
                    if event.unicode == u'q':
                        running = False
                    elif event.key == 275:
                        self.jog(1)
                    elif event.key == 276:
                        self.jog(-1)
                    elif event.unicode == u' ':
                        self.button()

            pygame.time.wait(50)

    def set_album(self, i):
        self.album = i
        album = self.albums[i]
        screen_w, screen_h = self.screen.get_size()
        screen_aspect = float(screen_w) / screen_h
        cover = pygame.image.load(album.cover).convert()
        cover_w, cover_h = cover.get_size()
        cover_aspect = float(cover_w) / cover_h
        if cover_aspect > screen_aspect:
            # Width is limiting factor, scale to width
            scale_w = screen_w
            scale_h = int(scale_w / cover_aspect)
        else:
            # Height is limiting factor, scale to height
            scale_h = screen_h
            scale_w = int(scale_h * cover_aspect)
        scaled = pygame.transform.smoothscale(cover, (scale_w, scale_h))
        self.screen.fill(BACKGROUND)
        self.screen.blit(scaled, scaled.get_rect())
        pygame.display.flip()

    def jog(self, i):
        if self.state == self.IDLE:
            self.set_album((self.album + i) % len(self.albums))
        elif self.state == self.PLAYING:
            if i > 0:
                subprocess.check_call(['mocp', '--next'])
            else:
                subprocess.check_call(['mocp', '--previous'])

    def button(self):
        if self.state == self.IDLE:
            self.play()
        else:
            self.stop()

    def play(self):
        print 'QUEUEING'
        subprocess.check_call(['mocp', '--clear'])
        tracks = [track.path for track in self.albums[self.album].tracks]
        subprocess.check_call(['mocp', '--append'] + tracks)
        print 'PLAY!'
        subprocess.check_call(['mocp', '--play'])
        self.state = self.PLAYING

    def stop(self):
        print 'STOP!'
        subprocess.check_call(['mocp', '--stop'])
        self.state = self.IDLE


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
    discnum = number(tags.get('discnumber'), 1)
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


_marker = object()


def number(s, default=_marker):
    """
    Yes, mutagen is this annoying.
    """
    if not s and default is not _marker:
        return default
    if isinstance(s, list):
        s = s[0]
    if '/' in s:
        s, total = s.split('/')
    if not s and default is not _marker:
        return default
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
    LilJuke(os.path.abspath(folder)).run(len(sys.argv) > 2)
