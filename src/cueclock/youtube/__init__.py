"""YouTube for the stream (#81): sign-in, the video's title, thumbnail and chapters.

`api` is the only module that touches the network; `title`, `thumbnail` and
`chapters` are pure, so everything a viewer reads is unit-tested without it.
"""
