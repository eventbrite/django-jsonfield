__version_info__ = (1, 4, 2, 'eventbrite')
__version__ = '+'.join(filter(None, ['.'.join(map(str, __version_info__[:3])), (__version_info__[3:] or [None])[0]]))