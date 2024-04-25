from IPython.display import display, Image, Audio

import os
from datetime import datetime, timedelta
from isodate import parse_duration
import requests
import base64
import time
from datetime import datetime as dt
import logging
#TODO: put in propper logging

from openai import OpenAI
from youtube_transcript_api import YouTubeTranscriptApi
from googleapiclient.discovery import build
import cv2  # We're using OpenCV to read video, to install !pip install opencv-python
from pytube import YouTube
#for debugging
import pprint
pp = pprint.PrettyPrinter(indent=2).pprint
import ipdb

# Create the logger for your application code
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
# Create a custom formatter
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
# Add a StreamHandler to logger and set the formatter
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
# Set the root logger to only log messages with a level of INFO or higher
# This will suppress debug logs from external libraries
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

def get_chapters_for_video(video_id):
	"""takes video_id and returns a list of dicts. Each dict corresponds to a chapter in the video"""
	#test what happens if there's no chapters
	#TODO: put in friendly error message if this API is not available.
	j = requests.get(f"https://yt.lemnoslife.com/videos?part=chapters&id={video_id}").json()
	chapters = j["items"][0]["chapters"]["chapters"]
	return [{"title": c["title"], "time": c["time"]} for c in chapters]

def download_video_from_youtube(link, fname="tmp_vid.mp4"):
	"""downloads the video at the link to the directory this notebook is running from."""
	youtubeObject = YouTube(link)
	youtubeObject = youtubeObject.streams.get_highest_resolution()
	#TODO: download the file to the videos/ folder
	youtubeObject.download(filename=fname)
	print("Download completed successfully")

def extract_frames_from_video(video_id, delete_source):
	video_fname = "tmp_vid.mp4"
	base_yt_url = "https://www.youtube.com/watch?v="
	video_url = f"{base_yt_url}{video_id}"
	download_video_from_youtube(video_url, video_fname)
	video = cv2.VideoCapture(video_fname)
	# extracting frames
	base64Frames = []
	i = 0
	while video.isOpened():
		success, frame = video.read()
		if not success:
			break
		_, buffer = cv2.imencode(".jpg", frame)
		base64Frames.append(base64.b64encode(buffer).decode("utf-8"))
		i+=1
		if i%1000 == 0:
			print(f"{dt.strftime(dt.now(), '%H:%M:%S')} {i} frames created")
	video.release()
	print(len(base64Frames), "frames read.")
	# deleting the video
	if delete_source:
		logger.debug(f"Removing {video_fname}")
		os.remove(video_fname)
	return base64Frames

def create_transcript_via_vision(video_id, delete_source=True):
	"""Takes a YouTube video_id and uses GPT vision to create a transcript of the video"""
	base64Frames = extract_frames_from_video(video_id, delete_source)
	client = OpenAI(api_key=openai_api_key)
	# passes only every 50 frames to gpt vision
	PROMPT_MESSAGES = [
		{
			"role": "user",
			"content": [
				"These are frames from a video that I want to upload. Create a summary of what is happening in the video.",
				*map(lambda x: {"image": x, "resize": 768}, base64Frames[0::50]),
			],
		},
	]
	params = {
		"model": "gpt-4-vision-preview",
		"messages": PROMPT_MESSAGES,
		"max_tokens": 200,
	}
	result = client.chat.completions.create(**params)
	logger.debug(result.choices[0].message.content)
	return result.choices[0].message.content

def get_video_transcript(video_id):
	try:
		transcript_with_timestamps = YouTubeTranscriptApi.get_transcript(video_id)
	except:
		#TODO: not great practice to hard-code the flag here.
		return create_transcript_via_vision(video_id)
	transcript = ' '.join([t['text'] for t in transcript_with_timestamps])
	return transcript

def get_videos_from_question(question="", days=365, maxResults=50, limiter=5, allow_shorts=False):
	#TODO: docstring
	#TODO: could probably split this up
	youtube_client = build('youtube', 'v3', developerKey=youtube_api_key)
	response = youtube_client.search().list(
		q=question,
		part='snippet',
		type='video',
		maxResults=maxResults
	).execute()
	videos = response['items']
	logger.debug(f"YT client returned {len(videos)} items")
	
	# Filter videos from the last 'x' days and get video IDs
	cutoff_date = datetime.now() - timedelta(days=days)
	recent_videos = []
	final_video_set = []
	for video in videos:
		if datetime.strptime(video['snippet']['publishedAt'], '%Y-%m-%dT%H:%M:%SZ') > cutoff_date:
			recent_videos.append(video)
	
	video_ids = [v['id']['videoId'] for v in recent_videos]
	# Get video durations
	response = requests.get(f'https://www.googleapis.com/youtube/v3/videos?part=contentDetails&id={",".join(video_ids)}&key={youtube_api_key}')
	# get the duration for each video in recent_videos
	video_durations = {video['id']: parse_duration(video['contentDetails']['duration']).total_seconds() for video in response.json()['items']}
	if not allow_shorts:
		recent_videos = [video for video in recent_videos if video_durations[video['id']['videoId']] > 60]

	logger.debug(f"after filtering on date and duration, have got {len(recent_videos)} items")
	if len(recent_videos) == 0:
		logger.debug("No videos to get. Returning empty")
		return []
	
	# trim down the number of results if desired
	if limiter:
		if len(recent_videos) > limiter:
			recent_videos = recent_videos[:limiter]

	for video in recent_videos:
		final_video = {
			'video_id': video['id']['videoId'],
			'title': video['snippet']['title'],
			'description': video['snippet']['description'],
			'thumbnail': video['snippet']['thumbnails']['high']['url'],
			'publishedAt': video['snippet']['publishedAt'],
			'transcript': get_video_transcript(video['id']['videoId']),
			'duration': f"{int(video_durations[video['id']['videoId']] // 60)}m {int(video_durations[video['id']['videoId']] % 60)}s",
			'chapters': get_chapters_for_video(video['id']['videoId'])			
		}
		final_video_set.append(final_video)
	return final_video_set

def summarise_transcript(video):
	#TODO: prob should move the prompts to their own files. Feels a bit brittle having so much string in a .py file.
	if len(video["chapters"]) > 0:
		prompt = f"""
		I am going to provide you with a video_id, the transcript of the video and its chapters.

		The chapters will be a python list of objects in the following format
		[{{"title": title, "time": chapter_timestamp}}]

		The chapter_timestamp corresponds to the time stamp of the chapter.

		Summarise this into 5 actions the audience can take that can be marked as complete, as a numbered list, no other content. Please make each one practical and specific.

		For each point, link to the corresponding chapter by placing the corresponding chapter_timestamp

		The summary should look like this
		* point 1 - https://www.youtube.com/watch?v={video["video_id"]}&t=<corresponding chapter_timestamp>s
		* point 2 - https://www.youtube.com/watch?v={video["video_id"]}&t=<corresponding chapter_timestamp>s
		* point 3- https://www.youtube.com/watch?v={video["video_id"]}&t=<corresponding chapter_timestamp>s
		* point 4 - https://www.youtube.com/watch?v={video["video_id"]}&t=<corresponding chapter_timestamp>s
		* point 5- https://www.youtube.com/watch?v={video["video_id"]}&t=<corresponding chapter_timestamp>s

		video_id: {video["video_id"]}

		chapters: {video["chapters"]}
		"""
	else:
		prompt = """
		Distill the following trancript down into 5 actions you can mark as complete, as a numbered list, no other content. Please make each one practical and specific.
		"""
	client = OpenAI(api_key=openai_api_key)

	response = client.chat.completions.create(
		model="gpt-3.5-turbo-16k",
		messages=[
			{"role": "system", "content": prompt},
			{"role": "user", "content": video["transcript"][:70000]}
		]
	)
	return response.choices[0].message.content

def ask_question(question, days, maxResults, limiter, allow_shorts):
	""""""
	videos = get_videos_from_question(question, days, maxResults, limiter, allow_shorts)
	if not videos:
		print("No videos were found. So, there's nothing to summarise.")
		return
	for video in videos:
		print(f"Summarised video: {video['title']}")
		print(summarise_transcript(video))
		print("")

if __name__ == "__main__":
	# limits the number of videos summarised
	LIMITER = 2
	# limits search results to the number of days specified. e.g. 365 will get only videos from last 365 days
	DAYS = 365
	MAXRESULTS = 50
	# whether you want to allow videos that are fewer than 60 seconds in duration.
	ALLOW_SHORTS = True
	#TODO: find why it isn't deleting the source
	openai_api_key = os.environ['OPEN_AI_KEY']
	youtube_api_key = os.environ['YOUTUBE_API_KEY']
	question = input("Hi. Ask me a question and I'll summarise YouTube's best videos on the subject.\n")
	ask_question(question, DAYS, MAXRESULTS, LIMITER, ALLOW_SHORTS)