# playlist-generator
# Summary and Workflow
Ever wanted to have a thematic playlist for every situation in life? Well here's a script for you!

This playlist generating script leverages the Spotify API (via Spotipy), Genius API, and Musicmatch API to do the following:
* Authenticate into *your* Spotify account via your browser to create a new playlist (or add to a preexisting playlist)
* Search Genius, Spotify, and/or (Musixmatch: TODO) lyrics/song databases for songs based on a provided search term
* Calculate the number of times your search term (or set of search terms) appears in the lyrics
* If the percentage of term occurences in the total lyrics meets a threshold, designate **BANGER** and add to the playlist (while avoiding duplicates)

Note, the songs are added to the playlist as you go, so if you kill the program early you will still get a playlist with progress up to the point at which you killed it. 

# Requirements/Pre-Requesites 
* Solid knowledge of how to use your command line
* Python3 
* lyricsgenius library 
  * `pip3 install lyricsgenius`
* spotipy library 
  * `pip3 install spotipy`
* The following environment variables:
  * [Genius API Secret](https://genius.com/signup_or_login) - set to `GENIUS_TOKEN=<your_secret_here>`
  
# Usage

## Help
As a general rule, the help command will give you information about parameters to use when executing the script
    python3 generate-playlist.py -h

## Basic Usage
Let's say you want to make a playlist about, say, hotdogs. The following command will do a search for hotdogs and create a playlist called "hotdog playlist". If a playlist called "hotdog playlist" already exists in your Spotify account, it will add the songs in the search to the preexisting list
    python3 generate-playlist.py -q hotdogs -t 'hotdog playlist'
