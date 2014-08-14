from flask import Flask, url_for
app = Flask(__name__)

@app.route('/<user>/<repository>')
def github(user, repository):
    return 'sudo docker.io build  -t image/idenitfier https://github.com/%s/%s.git' % (user, repository)

if __name__ == '__main__':
	with app.test_request_context():
		print url_for('github', user='hubx', repository='SWA-BAttack')
	app.run(debug=True)