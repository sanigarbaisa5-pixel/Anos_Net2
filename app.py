
import os
from datetime import datetime
from functools import wraps

import cloudinary
import cloudinary.uploader

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, abort, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, join_room
from sqlalchemy import or_
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-this-key")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

database_url = os.environ.get("DATABASE_URL", "sqlite:///anos_net_dev.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url

db = SQLAlchemy(app)

socketio = SocketIO(
    app,
    async_mode="threading",
    logger=False,
    engineio_logger=False
)

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)


def upload_image(file, folder):
    if not file or not file.filename:
        return None

    if not all([
        os.environ.get("CLOUDINARY_CLOUD_NAME"),
        os.environ.get("CLOUDINARY_API_KEY"),
        os.environ.get("CLOUDINARY_API_SECRET")
    ]):
        raise RuntimeError("Cloudinary n'est pas configuré.")

    result = cloudinary.uploader.upload(
        file,
        folder=f"anos_net/{folder}",
        resource_type="image",
        allowed_formats=["jpg", "jpeg", "png", "webp", "gif"]
    )
    return result["secure_url"]


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(30), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    avatar_url = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    caption = db.Column(db.String(1000), default="")
    image_url = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    author = db.relationship("User")
    likes = db.relationship("Like", backref="post", cascade="all, delete-orphan")
    comments = db.relationship("Comment", backref="post", cascade="all, delete-orphan")


class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("post_id", "user_id", name="uq_like_post_user"),
    )


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("post.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.String(1000), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    author = db.relationship("User")


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    body = db.Column(db.String(2000), default="")
    image_url = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    sender = db.relationship("User", foreign_keys=[sender_id])
    recipient = db.relationship("User", foreign_keys=[recipient_id])


def current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify(error="Connexion requise."), 401
            flash("Connecte-toi pour continuer.", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_current_user():
    return {"current_user": current_user()}


def private_room(user_a, user_b):
    low, high = sorted((int(user_a), int(user_b)))
    return f"private_{low}_{high}"


def message_payload(message):
    return {
        "id": message.id,
        "sender_id": message.sender_id,
        "sender": message.sender.username,
        "avatar_url": message.sender.avatar_url,
        "body": message.body or "",
        "image_url": message.image_url,
        "created_at": message.created_at.strftime("%d/%m %H:%M")
    }


@socketio.on("connect")
def socket_connect(auth=None):
    user = current_user()
    if not user:
        return False
    join_room("public_chat")
    join_room(f"user_{user.id}")


@socketio.on("join_private")
def socket_join_private(data):
    user = current_user()
    if not user or not isinstance(data, dict):
        return

    try:
        other_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return

    other = db.session.get(User, other_id)
    if not other or other.id == user.id:
        return

    # Only the two participants can join their private room.
    join_room(private_room(user.id, other.id))


@app.route("/")
def index():
    posts = Post.query.order_by(Post.created_at.desc()).limit(100).all()
    return render_template("index.html", posts=posts)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if len(username) < 3 or len(username) > 30:
            flash("Le pseudo doit contenir entre 3 et 30 caractères.", "danger")
        elif not username.replace("_", "").isalnum():
            flash("Utilise seulement des lettres, chiffres et _.", "danger")
        elif len(password) < 8:
            flash("Le mot de passe doit contenir au moins 8 caractères.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Ce pseudo est déjà utilisé.", "danger")
        else:
            user = User(
                username=username,
                password_hash=generate_password_hash(password)
            )
            db.session.add(user)
            db.session.commit()
            session.clear()
            session["user_id"] = user.id
            return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(
            username=request.form.get("username", "").strip()
        ).first()

        if user and check_password_hash(
            user.password_hash, request.form.get("password", "")
        ):
            session.clear()
            session["user_id"] = user.id
            return redirect(url_for("index"))

        flash("Pseudo ou mot de passe incorrect.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/search")
def search():
    query = request.args.get("q", "").strip()
    users = []
    if query:
        users = User.query.filter(
            User.username.ilike(f"%{query}%")
        ).order_by(User.username.asc()).limit(30).all()
    return render_template("search.html", users=users, query=query)


@app.route("/profile/<int:user_id>")
def profile(user_id):
    user = db.session.get(User, user_id) or abort(404)
    posts = Post.query.filter_by(user_id=user.id).order_by(
        Post.created_at.desc()
    ).all()
    return render_template("profile.html", user=user, posts=posts)


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    user = current_user()
    if request.method == "POST":
        image = request.files.get("avatar")
        try:
            if image and image.filename:
                user.avatar_url = upload_image(image, "avatars")
                db.session.commit()
                flash("Photo de profil mise à jour.", "success")
                return redirect(url_for("profile", user_id=user.id))
        except Exception:
            app.logger.exception("Erreur Cloudinary avatar")
            flash("Impossible d'envoyer la photo. Vérifie Cloudinary.", "danger")
    return render_template("edit_profile.html", user=user)


@app.route("/posts/create", methods=["POST"])
@login_required
def create_post():
    caption = request.form.get("caption", "").strip()[:1000]
    image = request.files.get("image")
    try:
        image_url = upload_image(image, "posts") if image and image.filename else None
    except Exception:
        app.logger.exception("Erreur Cloudinary publication")
        flash("Impossible d'envoyer l'image.", "danger")
        return redirect(url_for("index"))

    if not caption and not image_url:
        flash("Ajoute un texte ou une image.", "warning")
        return redirect(url_for("index"))

    db.session.add(Post(
        user_id=current_user().id,
        caption=caption,
        image_url=image_url
    ))
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/posts/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    post = db.session.get(Post, post_id) or abort(404)
    user_id = current_user().id
    like = Like.query.filter_by(post_id=post.id, user_id=user_id).first()

    if like:
        db.session.delete(like)
    else:
        db.session.add(Like(post_id=post.id, user_id=user_id))

    db.session.commit()
    return redirect(request.referrer or url_for("index"))


@app.route("/posts/<int:post_id>/comment", methods=["POST"])
@login_required
def comment_post(post_id):
    post = db.session.get(Post, post_id) or abort(404)
    body = request.form.get("body", "").strip()[:1000]
    if body:
        db.session.add(Comment(
            post_id=post.id,
            user_id=current_user().id,
            body=body
        ))
        db.session.commit()
    return redirect(request.referrer or url_for("index"))


@app.route("/chat")
@login_required
def public_chat():
    messages = Message.query.filter_by(recipient_id=None).order_by(
        Message.created_at.desc()
    ).limit(100).all()
    messages.reverse()
    return render_template("chat.html", messages=messages, recipient=None)


@app.route("/messages/<int:user_id>")
@login_required
def private_chat(user_id):
    me = current_user()
    recipient = db.session.get(User, user_id) or abort(404)

    if recipient.id == me.id:
        return redirect(url_for("profile", user_id=me.id))

    messages = Message.query.filter(
        or_(
            (Message.sender_id == me.id) & (Message.recipient_id == recipient.id),
            (Message.sender_id == recipient.id) & (Message.recipient_id == me.id)
        )
    ).order_by(Message.created_at.desc()).limit(200).all()
    messages.reverse()

    return render_template(
        "chat.html", messages=messages, recipient=recipient
    )


@app.route("/api/chat/public/send", methods=["POST"])
@login_required
def api_send_public():
    body = request.form.get("body", "").strip()[:2000]
    image = request.files.get("image")
    try:
        image_url = upload_image(image, "chat") if image and image.filename else None
    except Exception:
        app.logger.exception("Erreur Cloudinary chat public")
        return jsonify(error="Échec de l'envoi de l'image."), 400

    if not body and not image_url:
        return jsonify(error="Écris un message ou choisis une image."), 400

    message = Message(
        sender_id=current_user().id,
        recipient_id=None,
        body=body,
        image_url=image_url
    )
    db.session.add(message)
    db.session.commit()

    # Broadcast immediately to connected public-chat clients.
    socketio.emit("new_message", message_payload(message), to="public_chat")
    return jsonify(ok=True, message_id=message.id)


@app.route("/api/chat/private/<int:user_id>/send", methods=["POST"])
@login_required
def api_send_private(user_id):
    me = current_user()
    recipient = db.session.get(User, user_id) or abort(404)
    if recipient.id == me.id:
        return jsonify(error="Destinataire invalide."), 400

    body = request.form.get("body", "").strip()[:2000]
    image = request.files.get("image")
    try:
        image_url = upload_image(image, "private-chat") if image and image.filename else None
    except Exception:
        app.logger.exception("Erreur Cloudinary chat privé")
        return jsonify(error="Échec de l'envoi de l'image."), 400

    if not body and not image_url:
        return jsonify(error="Écris un message ou choisis une image."), 400

    message = Message(
        sender_id=me.id,
        recipient_id=recipient.id,
        body=body,
        image_url=image_url
    )
    db.session.add(message)
    db.session.commit()

    payload = message_payload(message)
    socketio.emit(
        "new_message",
        payload,
        to=private_room(me.id, recipient.id)
    )
    # Ensure both users can receive the private-room event even before opening it.
    socketio.emit("private_notice", payload, to=f"user_{recipient.id}")
    return jsonify(ok=True, message_id=message.id)


@app.route("/health")
def health():
    return jsonify(status="ok", app="ANOS NET")


@app.errorhandler(413)
def file_too_large(_error):
    if request.path.startswith("/api/"):
        return jsonify(error="Fichier trop volumineux. Maximum : 8 Mo."), 413
    flash("Fichier trop volumineux. Maximum : 8 Mo.", "danger")
    return redirect(request.referrer or url_for("index"))


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
        allow_unsafe_werkzeug=True
    )
