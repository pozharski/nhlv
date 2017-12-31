"""
"""

import logging
import os
import requests
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

import mlbam.auth as auth
import mlbam.util as util
import mlbam.config as config


LOG = logging.getLogger(__name__)


def is_fav(game_rec):
    if 'favourite' in game_rec:
        return game_rec['favourite']
    if config.CONFIG.parser['favs'] is None or config.CONFIG.parser['favs'] == '':
        return False
    for fav in config.CONFIG.parser['favs'].split(','):
        fav = fav.strip()
        if fav in (game_rec['away_abbrev'], game_rec['home_abbrev']):
            return True
    return False


def filter_favs(game_rec):
    """Returns the game_rec if the game matches the favourites, or if no filtering is active."""
    if not config.CONFIG.parser.getboolean('filter', 'false'):
        return game_rec
    if config.CONFIG.parser['favs'] is None or config.CONFIG.parser['favs'] == '':
        return game_rec
    for fav in config.CONFIG.parser['favs'].split(','):
        fav = fav.strip()
        if fav in (game_rec['away_abbrev'], game_rec['home_abbrev']):
            return game_rec
    return None


def select_feed_for_team(game_rec, team_code, feedtype=None):
    found = False
    if game_rec['away_abbrev'] == team_code:
        found = True
        if feedtype is None and 'away' in game_rec['feed']:
            feedtype = 'away'  # assume user wants their team's feed
    elif game_rec['home_abbrev'] == team_code:
        found = True
        if feedtype is None and 'home' in game_rec['feed']:
            feedtype = 'home'  # assume user wants their team's feed
    if found:
        if feedtype is None:
            LOG.info('Default (home/away) feed not found: choosing first available feed')
            if len(game_rec['feed']) > 0:
                feedtype = list(game_rec['feed'].keys())[0]
                LOG.info("Chose '{}' feed (override with --feed option)".format(feedtype))
        if feedtype not in game_rec['feed']:
            LOG.error("Feed is not available: {}".format(feedtype))
            return None, None
        return game_rec['feed'][feedtype]['mediaPlaybackId'], game_rec['feed'][feedtype]['eventId']
    return None, None


def find_highlight_url_for_team(game_rec, feedtype):
    if feedtype not in config.HIGHLIGHT_FEEDTYPES:
        raise Exception('highlight: feedtype must be condensed or recap')
    if feedtype in game_rec['feed'] and 'playback_url' in game_rec['feed'][feedtype]:
        return game_rec['feed'][feedtype]['playback_url']
    LOG.error('No playback_url found for {} vs {}'.format(game_rec['away_abbrev'], game_rec['home_abbrev']))
    return None


def fetch_stream(game_pk, content_id, event_id):
    """ game_pk: game_pk
        event_id: eventId
        content_id: mediaPlaybackId
    """
    stream_url = None
    media_auth = None

    auth_cookie = auth.get_auth_cookie()
    if auth_cookie is None:
        LOG.error("fetch_stream: not logged in")
        return stream_url, media_auth

    session_key = auth.get_session_key(game_pk, event_id, content_id, auth_cookie)
    if session_key is None:
        return stream_url, media_auth
    elif session_key == 'blackout':
        msg = ('The game you are trying to access is not currently available due to local '
               'or national blackout restrictions.\n'
               ' Full game archives will be available 48 hours after completion of this game.')
        LOG.info('Game Blacked Out: {}'.format(msg))
        return stream_url, media_auth

    url = config.CONFIG.mf_svc_url
    url += '?contentId=' + content_id
    url += '&playbackScenario=' + config.CONFIG.playback_scenario
    url += '&platform=' + config.CONFIG.platform
    url += '&sessionKey=' + urllib.parse.quote_plus(session_key)

    # Get user set CDN
    if config.CONFIG.parser['cdn'] == 'akamai':
        url += '&cdnName=MED2_AKAMAI_SECURE'
    elif config.CONFIG.parser['cdn'] == 'level3':
        url += '&cdnName=MED2_LEVEL3_SECURE'

    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "keep-alive",
        "Authorization": auth_cookie,
        "User-Agent": config.CONFIG.svc_user_agent,
        "Proxy-Connection": "keep-alive"
    }

    util.log_http(url, 'get', headers, sys._getframe().f_code.co_name)
    r = requests.get(url, headers=headers, cookies=auth.load_cookies(), verify=config.VERIFY_SSL)
    json_source = r.json()

    if json_source['status_code'] == 1:
        media_item = json_source['user_verified_event'][0]['user_verified_content'][0]['user_verified_media_item'][0]
        if media_item['blackout_status']['status'] == 'BlackedOutStatus':
            msg = ('The game you are trying to access is not currently available due to local '
                   'or national blackout restrictions.\n'
                   'Full game archives will be available 48 hours after completion of this game.')
            util.die('Game Blacked Out: {}'.format(msg))
        elif media_item['auth_status'] == 'NotAuthorizedStatus':
            msg = 'You do not have an active subscription. To access this content please purchase a subscription.'
            util.die('Account Not Authorized: {}'.format(msg))
        else:
            stream_url = media_item['url']
            media_auth = '{}={}'.format(str(json_source['session_info']['sessionAttributes'][0]['attributeName']),
                                        str(json_source['session_info']['sessionAttributes'][0]['attributeValue']))
            session_key = json_source['session_key']
            auth.update_session_key(session_key)
    else:
        msg = json_source['status_message']
        util.die('Error Fetching Stream: {}', msg)

    LOG.debug('fetch_stream stream_url: ' + stream_url)
    LOG.debug('fetch_stream media_auth: ' + media_auth)
    return stream_url, media_auth


def save_playlist_to_file(stream_url, media_auth):
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.8",
        "Connection": "keep-alive",
        "User-Agent": config.CONFIG.svc_user_agent,
        "Cookie": media_auth
    }
    util.log_http(stream_url, 'get', headers, sys._getframe().f_code.co_name)
    r = requests.get(stream_url, headers=headers, cookies=auth.load_cookies(), verify=config.VERIFY_SSL)
    playlist = r.text
    playlist_file = os.path.join(config.CONFIG.dir, 'playlist-{}.m3u8'.format(time.strftime("%Y-%m-%d")))
    LOG.debug('writing playlist to: {}'.format(playlist_file))
    with open(playlist_file, 'w') as f:
        f.write(playlist)
    LOG.debug('save_playlist_to_file: {}'.format(playlist))


def play_stream(game_data, team_to_play, feedtype, date_str, record, login_func):
    game_rec = None
    for game_pk in game_data:
        if team_to_play in (game_data[game_pk]['away_abbrev'], game_data[game_pk]['home_abbrev']):
            game_rec = game_data[game_pk]
            break
    if game_rec is None:
        util.die("No game found for team {}".format(team_to_play))

    if feedtype is not None and feedtype in config.HIGHLIGHT_FEEDTYPES:
        # handle condensed/recap
        playback_url = find_highlight_url_for_team(game_rec, feedtype)
        if playback_url is None:
            util.die("No playback url for feed '{}'".format(feedtype))
        run_streamlink_highlight(playback_url, get_recording_filename(date_str, game_rec, feedtype, record))
    else:
        # handle full game (live or archive)
        # this is the only feature requiring an authenticated session
        auth_cookie = auth.get_auth_cookie()
        if auth_cookie is None:
            login_func()
            # auth.login(config.CONFIG.parser['username'],
            #            config.CONFIG.parser['password'],
            #            config.CONFIG.parser.getboolean('use_rogers', False))
        LOG.debug('Authorization cookie: {}'.format(auth.get_auth_cookie()))

        media_playback_id, event_id = select_feed_for_team(game_rec, team_to_play, feedtype)
        if media_playback_id is not None:
            stream_url, media_auth = fetch_stream(game_rec['game_pk'], media_playback_id, event_id)
            if stream_url is not None:
                if config.DEBUG:
                    save_playlist_to_file(stream_url, media_auth)
                run_streamlink(stream_url, media_auth,
                               get_recording_filename(date_str, game_rec, feedtype, record))
            else:
                LOG.error("No stream URL")
        else:
            LOG.info("No game found for {}".format(team_to_play))
    return 0


def get_recording_filename(date_str, game_rec, feedtype, record):
    if record:
        return '{}-{}-{}-{}.mp4'.format(date_str, game_rec['away_abbrev'], game_rec['home_abbrev'], feedtype)
    else:
        return None


def run_streamlink_highlight(playback_url, record_filename):
    video_player = config.CONFIG.parser['video_player']
    streamlink_cmd = ["streamlink", "--player-no-close", ]
    if record_filename is not None:
        streamlink_cmd.append("--output")
        streamlink_cmd.append(record_filename)
    elif video_player is not None and video_player != '':
        LOG.debug('Using video_player: {}'.format(video_player))
        streamlink_cmd.append("--player")
        streamlink_cmd.append(video_player)
    if config.VERBOSE:
        streamlink_cmd.append("--loglevel")
        streamlink_cmd.append("debug")
    streamlink_cmd.append(playback_url)
    streamlink_cmd.append(config.CONFIG.parser.get('resolution', 'best'))

    LOG.info('Playing highlight: ' + str(streamlink_cmd))
    subprocess.run(streamlink_cmd)


def run_streamlink(stream_url, media_auth, record_filename=None):
    LOG.info("Stream url: " + stream_url)
    auth_cookie_str = "Authorization=" + auth.get_auth_cookie()
    media_auth_cookie_str = media_auth
    user_agent_hdr = 'User-Agent=' + config.CONFIG.ua_iphone

    video_player = config.CONFIG.parser['video_player']
    streamlink_cmd = ["streamlink", 
                      "--http-no-ssl-verify",
                      "--player-no-close",
                      "--http-cookie", auth_cookie_str,
                      "--http-cookie", media_auth_cookie_str,
                      "--http-header", user_agent_hdr]
    if record_filename is not None:
        streamlink_cmd.append("--output")
        streamlink_cmd.append(record_filename)
    elif video_player is not None and video_player != '':
        LOG.debug('Using video_player: {}'.format(video_player))
        streamlink_cmd.append("--player")
        streamlink_cmd.append(video_player)
    if config.VERBOSE:
        streamlink_cmd.append("--loglevel")
        streamlink_cmd.append("debug")
    streamlink_cmd.append(stream_url)
    streamlink_cmd.append(config.CONFIG.parser.get('resolution', 'best'))

    LOG.info('Playing: ' + str(streamlink_cmd))
    subprocess.run(streamlink_cmd)

    return streamlink_cmd
