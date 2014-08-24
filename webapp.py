from flask import Flask, url_for, redirect
from flask import Response, stream_with_context
import subprocess
import random
import httplib
import requests

from celery_task import make_celery

app = Flask("webapp")
app.config.update(
    CELERY_BROKER_URL = 'sqla+sqlite:///celerydb.sqlite',
    CELERY_RESULT_BACKEND = 'db+sqlite:///results.sqlite'
)
celery = make_celery(app)

@app.route('/<user>/<repository>')
def github(user, repository):
	if not repository_allowed(user, repository):
		return "repository not allowed"
	if not repository_exists(user, repository):
		return "repository does not exist"
	result = live_instace.delay(user, repository)
	return redirect(url_for('status_for', id=result.id))

def repository_allowed(user, repository):
	repo = "https://github.com/%s/%s" % (user, repository)
	for r in GH_REPOSITORIES:
		if repo.startswith(r):
			print repo + " starts with " + r
			return True
	return False

def repository_exists(user, repository):
	url = "/" + user + "/" + repository
	try:
		conn = httplib.HTTPSConnection("github.com")
		conn.request("HEAD", url)
		r = conn.getresponse()
		r.read()
		return 200 == r.status
	except StandardError as e:
		print e
		return None

@app.route('/status/<id>')
def status_for(id):
	r = live_instace.AsyncResult(id)
	if r.ready():
		return redirect('http://localhost:5000/static/noVNC/vnc.html?autoconnect=true&host=localhost&password=1234&path=&port='+
			str(r.get()))
	else:
		return '<script>setTimeout(function(){window.location.reload(1);}, 10000);</script>booting up'

@app.route('/image/<id>')
def get_image_for(id):
	r = live_instace.AsyncResult(id)
	if r.ready():
		req = requests.get("http://localhost:"+str(int(r.get())+1)+"/Squeak4.5-13680.image", stream = True)
   		return Response(stream_with_context(req.iter_content()), content_type = req.headers['content-type'])
   	return "not ready yet"

@app.route('/changes/<id>')
def get_changes_for(id):
	pass

@celery.task(track_started=True)
def delete_instance(instance):
	try:
		subprocess.check_call([	"sudo", "docker.io", "stop",
								instance])
		subprocess.check_call([	"sudo", "docker.io", "rm",
								instance])
	except subprocess.CalledProcessError as e:
		print "[ERROR] Could not stop image: " + str(e)

@celery.task(track_started=True)
def live_instace(user, repository):
	commit = build_image(user, repository)
	return run_image(user, repository, commit)

def build_image_cache(user, repository, commit):
	project = "%s/%s" % (user, repository)

	p = subprocess.check_output(['sudo', "docker.io", "images"])
	for line in p.split('\n'):
		if project.lower() in line:
			if ' ' + commit + ' ' in line:
				print "cache hit for", project, ":", commit
				return
				#XX add real commit, HEAD will result in false positives cache hits
	subprocess.check_call(["sudo", "docker.io", "build", "-t", project.lower() + ":" + commit,
							"https://github.com/" + project + ".git"])

def build_image(user, repository, commit="HEAD"):
	try:
		build_image_cache(user, repository, commit)

	except subprocess.CalledProcessError as e:
		print "[ERROR] Could not build image: " + str(e)

	return commit

def run_image(user, repository, commit):
	#xx limit to 50 parallel sessions
	project = "%s/%s" % (user, repository)
	#xx choose free instance name
	instance = user + "-" + repository + "-" + str(random.randint(1, 100)) + "-" + commit

	#xx choose free port
	port = str(random.randint(5900, 15900))
	try:
		subprocess.check_call([	"sudo", "docker.io", "run", "-d", 
								"--name", instance,
								"-p", port+":8080",
								"-p", str(int(port)+1) + ":80",
								"-c", "100", # equals 10% cpu shares
								project.lower() + ":" + commit])
		print port
	except subprocess.CalledProcessError as e:
		print "[ERROR] Could not start image: " + str(e)

	delete_instance.apply_async([instance], countdown=3660)
	return port

#xx provide link to DockerImage
#tests
#replace http lib

if __name__ == '__main__':
	with open('github/allowed_repositories') as f:
		GH_REPOSITORIES = f.read().splitlines()
		GH_REPOSITORIES = [r for r in GH_REPOSITORIES if r.strip() != '']


	with app.test_request_context():
		print url_for('github', user='hubx', repository='SWA-BAttack')
		print repository_exists('hubx', 'SWA-BAttack')
		print repository_exists('hubx', 'SWA-BAttacks')
	app.run(debug=True)

