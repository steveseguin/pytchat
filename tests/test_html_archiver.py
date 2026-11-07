import copy
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock, patch

from pytchat.processors.html_archiver import HTMLArchiver


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.spans = []
        self.tags = []
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == 'td':
            self.cells.append('')
            self.in_cell = True
        if tag == 'span':
            self.spans.append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == 'td':
            self.in_cell = False

    def handle_data(self, data):
        if self.in_cell:
            self.cells[-1] += data


class TestHTMLArchiver(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        fixture = Path(__file__).parent / 'testdata/default/textmessage.json'
        self.action = json.loads(fixture.read_text(encoding='utf-8'))[
            'continuationContents']['liveChatContinuation']['actions'][0]
        # Archive tests exercise real processing and file output, without HTTP.
        with patch('pytchat.processors.html_archiver.httpx.Client'):
            self.writer = HTMLArchiver(str(Path(self.directory.name) / 'chat.html'))
        self.temp_file = self.writer.tmp_fp
        self.writer._encode_img = Mock(return_value='aW1hZ2U=')
        self.addCleanup(self.cleanup_writer)

    def cleanup_writer(self):
        self.temp_file.close()
        self.writer.executor.shutdown(wait=True)
        if Path(self.writer.tmp_filename).exists():
            Path(self.writer.tmp_filename).unlink()

    def component(self, message='hello', author='author', emoji=None):
        action = copy.deepcopy(self.action)
        item = action['addChatItemAction']['item']['liveChatTextMessageRenderer']
        item['message']['runs'] = [{'text': message}]
        item['authorName']['simpleText'] = author
        if emoji is not None:
            item['message']['runs'].append({'emoji': {
                'emojiId': '123test', 'shortcuts': [emoji],
                'image': {'thumbnails': [{'url': 'https://example.invalid/emoji.png'}]}
            }})
        return [{'chatdata': [action], 'timeout': 0}]

    def read_table(self):
        self.writer.finalize()
        table = TableParser()
        table.feed(Path(self.writer.save_path).read_text(encoding='utf-8'))
        return table

    def test_message_preserves_literal_markup_and_entities(self):
        message = 'Show <b>literal</b> &amp; text'
        self.writer.process(self.component(message=message))
        table = self.read_table()
        self.assertEqual(table.cells[3], message)
        self.assertNotIn('b', table.tags)

    def test_author_preserves_literal_markup_and_entities(self):
        author = '<b>Alice</b> &amp; Bob'
        self.writer.process(self.component(author=author))
        table = self.read_table()
        self.assertEqual(table.cells[2], author)
        self.assertNotIn('b', table.tags)

    def test_chat_markup_remains_text(self):
        message = '<script>/* inert archive text */</script>'
        self.writer.process(self.component(message=message))
        table = self.read_table()
        self.assertEqual(table.cells[3], message)
        self.assertNotIn('script', table.tags)

    def test_emoji_title_remains_one_attribute(self):
        label = ':wave:" data-extra="literal &amp;'
        self.writer.process(self.component(emoji=label))
        table = self.read_table()
        self.assertEqual(table.spans, [{'class': 'Z123test', 'title': label}])

    def test_plain_unicode_and_repeated_batches(self):
        for index in range(3):
            result = self.writer.process(self.component('Grüße 😀 ' + str(index)))
            self.assertEqual(result, (self.writer.save_path, index + 1))
        table = self.read_table()
        self.assertEqual(table.cells[3::7], ['Grüße 😀 ' + str(i) for i in range(3)])
        self.assertEqual(len(table.cells), 21)

    def test_emoji_spans_and_caching_are_preserved(self):
        for _ in range(2):
            self.writer.process(self.component('hello ', emoji=':wave:'))
        table = self.read_table()
        self.assertEqual(table.cells[3::7], ['hello ', 'hello '])
        self.assertEqual(table.spans, [{'class': 'Z123test', 'title': ':wave:'}] * 2)
        self.assertEqual(self.writer._encode_img.call_count, 1)

    def test_empty_archive(self):
        for component in (None, [], [None], [{'chatdata': []}]):
            self.assertEqual(self.writer.process(component), (self.writer.save_path, 0))
        self.assertEqual(self.read_table().cells, [])
