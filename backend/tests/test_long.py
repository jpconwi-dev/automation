from .test_duration import _probe, _run


def test_five_minute_video_is_five_minutes(env):
    # the 'model' writes 20% too little and the voice is a bit slow: the hardest realistic case for a 5-minute video
    job, project, video = _run(env, duration=300, aspect_ratio="16:9", wps=2.3, ratio=0.8, tone="calm")
    assert job["status"] == "completed", job["error"]
    assert abs(_probe(video, "v:0") - 300) < 0.3
    assert abs(_probe(video, "a:0") - 300) < 0.3
    print("video_seconds:", project["video_seconds"])
