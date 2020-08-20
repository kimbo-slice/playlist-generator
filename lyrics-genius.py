import lyricsgenius, pprint, re, string, argparse, spotipy, requests, os
from spotipy.oauth2 import SpotifyOAuth

##Input a search term, playlist name
parser = argparse.ArgumentParser()
parser.add_argument("--query", "-q", help="Search term for your playlist", required=True)
parser.add_argument("--title", "-t", help="Title of your playlist")
parser.add_argument("--lyricsMatch", "-l", help="Specify if you want to just match lyrics instead of lyrics and song title", required=False)
parser.add_argument("--songTitleMatch", "-s", help="Specify if you want to just match song title instead of lyrics and song title", required=False)
parser.add_argument("--playlistId", "-p", help="Specify Spotify playlist ID if you want to add to a playlist that is already created")
args = parser.parse_args()

trackIds = set()
playlistID = ""
searches = ['graham', 'grahame', 'graeme']
main_search = args.query
threshold = 1.5
page = 1

#Genius just returns an empty set of values when it runs out of searches, so this checks for that
#so we don't spin for a while
def ran_out_of_hits_genius(response) :
    hits = response['sections']
    for hit in hits :
        if len(hit['hits']) and (hit['type'] == 'song' or hit['type'] == 'lyric') :
            return False
    print("Ran out of hits!")
    return True

def set_active_playlist() :
    if args.playlistId :
        #Check to see if the playlist exists and has tracks
        try :
            playlistTracks = sp.playlist_tracks(args.playlistId)
            print("Using existing playlist with id {}".format(args.playlistId))
            #avoiding duplicate songs
            for track in playlistTracks['items'] :
                trackIds.add(track['track']['id'])
            return args.playlistId
        except :
            print("Could not find playlist with id {}".format(args.playlistId))
            print("Creating playlist with title of search term instead")
            return create_playlist(args.query)
    elif args.title :
        return create_playlist(args.title)
    else :
        print("Playlist title or id not specified - using query as title")
        return create_playlist(args.query)

#Creates a new Spotify playlist given a title and saves the ID for easy song adding
def create_playlist(title) :
    #Check to see if the playlist exists and has tracks
    playlists_response = sp.current_user_playlists()      
    for playlist in playlists_response['items'] : 
        if playlist['public'] == True and playlist['name'] == title:
            print("Public playlist with given title already exists - adding new tracks to it")
            playlistTracks = sp.playlist_tracks(playlist['id'])
            for track in playlistTracks['items'] :
                trackIds.add(track['track']['id'])
            return playlist['id']

    print("Creating playlist {} with search term {}".format(title ,args.query))
    #Make the list
    playlistResponse = sp.user_playlist_create(sp.me()['id'], title, public=True, description="Smart playlist supplied by beast infection")
    playlistID = playlistResponse['id']
    return playlistID

#Is it a banger?
def is_banger(lyrics, threshold) :
    re.sub('[^A-Za-z0-9 ]+', '', lyrics)
    totalChars = len(lyrics)
    lyric = lyrics.translate(str.maketrans('', '', string.punctuation)).lower()
    countChars = 0
    for search in searches :
        countChars += (lyric.count(search) * len(search))
    if totalChars == 0 :
        print("Lyrics restricted")
        return False
    percentage = countChars / totalChars * 100
    if percentage < threshold :
        print("does not meet threshold")
        print('Percentage: {}'.format(percentage))
        return False
    else :
        print("MATCH!!!")
        print('Percentage: {}'.format(percentage))
        return True

#Gets the lyrics from Genius and accounts for some weirdness with error handling
def get_lyrics_from_genius(title, artist) :
    try :
        lyrics_song = genius.search_song(title, artist=artist, get_full_info=False)
        if lyrics_song is None :
            return ""
        else :
            return lyrics_song.lyrics
    except :
        "error searching songs"
        return ""

#Given the set of track IDs in the initial playlist and those added during the search, adds
#songs to playlist
def add_unique_song_to_playlist(title, artist) :
    spotifyTrackMatch = sp.search(q='track:{} artist:{}'.format(title, artist), type='track')
    if spotifyTrackMatch['tracks'].get('total') != 0 :
        data = spotifyTrackMatch['tracks']['items']
        if data[0].get('id') not in trackIds :
            trackIds.add(data[0].get('id'))
            singleTrack = []
            singleTrack.append(data[0].get('id'))
            sp.user_playlist_add_tracks(user=sp.me()['id'], playlist_id=playlistID, tracks=singleTrack)

#Use Genius as the search engine - can match on lyrics and song title
def from_genius() :
    print("Using GENIUS to search")
    global page
    response = genius.search_genius_web(main_search, per_page=5, page=page)
    while not ran_out_of_hits_genius(response) :
        hits = response['sections']
        for hit in hits :
            for song in hit.get('hits') :
                if (song['index'] == 'lyric' and not args.songTitleMatch) or (song['index'] == 'song' and not args.lyricsMatch) :
                    title = song['result']['title']
                    artist = song['result']['primary_artist']['name']
                    lyrics = get_lyrics_from_genius(title, artist)
                    if not is_banger(lyrics, threshold) :
                        continue
                    else :
                        add_unique_song_to_playlist(title, artist)
        page += 1
        print("Left off on {}".format(page))
    print('Num added to playlist: {}'.format(len(trackIds)))
    print('Left off on page: {}'.format(page))

#Uses Spotify as the search engine - can match only on song title
def from_spotify() :
    print("Adding songs from Spotify search")

#Uses Musicmatch as the search engine - can match on lyrics and song title
def from_musicmatch() :
    print("Adding songs from musicmatch")

#Spotify authentication
print("Authenticating with Spotify")
scope = "playlist-modify-public"
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(show_dialog=True, scope=scope, cache_path='cache.txt'))

#Create the playlist or make sure a specified playlist actually exists
print("Setting the active playlist")
playlistID = set_active_playlist()

#Genius authentication
client_access_token = os.environ.get('GENIUS_TOKEN')
genius = lyricsgenius.Genius(client_access_token)
#Genius search and match
from_genius()

#Spotify search and match

#Musicmatch search and match

