r"""A tool to download remote image URLs as local files and replace them in Markdown.

This tool is designed to aid in migrating from Roam Research to Logseq. It analyzes a collection of

As an example, after importing Roam data to Logseq, you can run the following dry-run:

python markdown_image_downloader.py \
  --input_pattern='/Users/clayt/Documents/PKM/Hive/*/*.md' \
  --url_substring_filters=firebasestorage.googleapis.com \
  --image_dest_dir=/Users/clayt/Documents/PKM/Hive/assets \
  --markdown_dest_dir='../assets' \
  --dry_run

This wll print the execution plan, but will not modify any files or download any images. It is worth noting that if you
are using a standard Logseq file organization, `--input_pattern=/path/to/database/*/*.md` will capture all Markdown
files in the database, namely those in `pages/` and `journals/`. Similarly, `--image_dest_dir` should point to the
`assets/` directory to serve as the download location for image — this should match --markdown_dest_dir='../assets' as
well, which provides the directory string to build local image paths in the Markdown source. In this example,
`--url_substring_filters=firebasestorage.googleapis.com` is a simple filter to restrict images to only those in the
Firebase domain, which is where Roam stores images (this works for me, but if there are other Firebase images in your
database which are not from Roam, you may want to further restrict this filter).

It is wise to backup your database before running real execution. To execute edits, run the same command without
`--dry_run`, i.e.:

python markdown_image_downloader.py \
  --input_pattern='/Users/clayt/Documents/PKM/Hive/*/*.md' \
  --url_substring_filters=firebasestorage.googleapis.com \
  --image_dest_dir=/Users/clayt/Documents/PKM/Hive/assets \
  --markdown_dest_dir='../assets'

"""
import collections
import dataclasses
import glob
import imghdr
import io
import os
import re
import shutil
import textwrap
import time
import urllib

from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from absl import app
from absl import flags
from absl import logging
import marko
import requests


_INPUT_PATTERN = flags.DEFINE_string('input_pattern', None, 'Pattern to match input files.', required=True)
_URL_SUBSTRING_FILTERS = flags.DEFINE_list('url_substring_filters', None,
                                           'If provided, only URLs containing at least one substring are operated on.')
_IMAGE_DEST_DIR = flags.DEFINE_string('image_dest_dir', None,
                                      'Full path to the destination directory to save images.', required=True)
_MARKDOWN_DEST_DIR = flags.DEFINE_string('markdown_dest_dir', None,
                                         'The directory string to substitute in the Markdown source.', required=True)
_PLAN_SUMMARY = flags.DEFINE_string('plan_summary',
                                    '/tmp/markdown_image_download_plan_summary.md',
                                    'Path to plan summary. This is a description of what edits are planned based on'
                                    'analysis of the input Markdown files.')
_EXECUTION_SUMMARY = flags.DEFINE_string('execution_summary',
                                         '/tmp/markdown_image_download_execution_summary.md',
                                         'Path to execution summary. This records image download failures and cases '
                                         'for which the image replacement failed. Such cases need manual cleanup.')
_DRY_RUN = flags.DEFINE_bool('dry_run', False,
                             'If set, prints the plan summary to stdout but does not download images or modify source.'
                             'The same information is saved in --plan_summary as well.')


def build_url_dest_regex(url):
  """Builds a regex pattern which matches a literal URL surrounded by ()'s with some possible whitespace."""
  return f'\(\s*{re.escape(url)}\s*\)'


def collect_image_elements(element) -> Sequence[marko.inline.Image]:
  """Collects all descendent images of an element."""
  if isinstance(element, marko.inline.Image):
    return [element]
  elif hasattr(element, 'children'):
    imgs = []
    for child in element.children:
      imgs += collect_image_elements(child)
    return imgs
  else:
    return []


def get_image_url_counts(md_source: str) -> Mapping[str, int]:
  """Gets the occurrence count of each image URL in a Markdown source."""
  parser = marko.parser.Parser()
  doc = parser.parse(md_source)
  imgs = collect_image_elements(doc)

  # Filter out image elements that do not have URL destinations.
  imgs = [img for img in imgs if img.dest.startswith('http')]

  return collections.Counter([img.dest for img in imgs])


def get_textual_counts(md_source: str, urls: Iterable[str]) -> Mapping[str, int]:
  """Gets the number of textual occurrences of a set of URLs in a Markdown file.

  This matches instances of the regex '\(\s*URL\s*\)' where 'URL' is the URL in question.
  In other words, this matches instances of URLs which are wrapped in ()'s with some whitespace allowed.

  Args:
    md_source: The Markdown source.
    urls: URLs for which to count occurrences.

  Returns:
    The occurrence count for each given URL.
  """
  url_textual_counts = {}
  for url in urls:
    url_textual_counts[url] = len(re.findall(build_url_dest_regex(url), md_source))
    # url_textual_counts[url] = md_source.count(url)
  return url_textual_counts


class LogseqImageFilenameTransformer:
  """Assigns unique, local image filenames in the Logseq style."""

  def __init__(self, existing_filenames: Sequence[str] = set()):
    """Initializes with a set of existing filenames to avoid duplicates."""
    self._existing_filename_roots = {os.path.splitext(os.path.basename(fn))[0] for fn in existing_filenames}

  def get_uniquified_filename(self, filename: str) -> Tuple[str, str]:
    """Creates a unique filename given an original filename.

    1) Spaces are replaced by '_' in the filename.
    2) The unix epoch is appended to the filename.
    3) A uniquifying suffix (e.g. '_0') is appended to the filename.
    4) The file extension is kept as is.

    Filename uniqueness considers only the basename excluding the extension.
    E.g. 'file1.jpg' and 'file1.png' are considered the same. This allows
    assigning unique filenames before the correct extension is known.

    Args:
      filename: The original filename.

    Returns:
      A transformed filename root which is unique within the set of pre-existing files as (root, ext).
    """
    root, ext = os.path.splitext(filename)
    root.replace(' ', '_')
    root += '_' + str(int(time.time()))
    ext = ext.replace('.', '')  # For consistency elsewhere, we store the extension without the ".".

    candidate_unique_suffix = 0
    while True:
      candidate_filename_root = f'{root}_{candidate_unique_suffix}'
      if candidate_filename_root not in self._existing_filename_roots:
        break
      else:
        candidate_unique_suffix += 1
    return candidate_filename_root, ext

  def assign_uniquified_filename(self, filename) -> Tuple[str, str]:
    """Assigns a unique filename and records the filename root in the existing set."""
    root, ext = self.get_uniquified_filename(filename)
    self._existing_filename_roots.add(root)
    return root, ext


@dataclasses.dataclass
class FileOccurrenceRecord:
  filepath: str = ''  # The Markdown filepath.
  num_image_elements: int = 0  # The number of image elements referencing this URL.
  num_extra_textual_occurrences: int = 0  # The number of URL references that are not by an image element.
  replace_successful: bool = False  # Whether the URL was replaced in this file.

  def replacement_unsafe(self):
    """Returns whether replacing URL occurrences in this file is safe.

    Replacement is considered safe iff it affects only image elements in the file.
    """
    return self.num_extra_textual_occurrences > 0

  def __str__(self):
    return (f'filepath: {self.filepath}\n'
            f'num_image_elements: {self.num_image_elements}\n'
            f'num_extra_textual_occurrences: {self.num_extra_textual_occurrences}\n')


@dataclasses.dataclass
class ImageUrlRecord:
  """A record of an image URL that occurs in a Markdown file."""

  url: str = ''
  passes_filters: bool = False
  original_filename: str = ''
  local_basename: str = ''
  local_ext: str = ''
  download_successful: bool = False
  file_occurrences: List[FileOccurrenceRecord] = dataclasses.field(default_factory=list)

  def local_filename(self):
    """Gets the local filename for this image."""
    return f'{self.local_basename}.{self.local_ext}' if self.local_ext else self.local_basename

  def get_file_occurrence(self, filepath) -> Optional[FileOccurrenceRecord]:
    """Gets the file occurrence record for this image URL the given filepath."""
    for file_occurrence in self.file_occurrences:
      if file_occurrence.filepath == filepath:
        return file_occurrence
    return None

  def download(self, local_dir: str):
    """Downloads the image and saves in local_dir.

    Args:
      local_dir: The destination of the downloaded image file.
    """
    if not local_dir:
      raise ValueError('local_dir is empty in ImageUrlRecord.download_image.')
    if not self.local_basename:
      raise ValueError('ImageUrlRecord does not have an assigned local_basename.')

    # If the file extension is known from the URL, we stream the image data directly to the local file.
    if self.local_ext:
      logging.info('Downloading %s by streaming to file...', self.url)
      response = requests.get(self.url, stream=True, allow_redirects=True)
      if response.status_code != 200:
        logging.error('Failed downloading %s with status $d.', self.url, response.status_code)
        return
      response.raw.decode_content = True

      image_file = response.raw

    # If the file extension is not known, we download the image in memory first, inspect the image data to determine
    # the correct file extension, and then save the image data to disk.
    else:
      logging.info('Downloading %s in-memory...', self.url)
      response = requests.get(self.url, stream=False, allow_redirects=True)
      if response.status_code != 200:
        logging.error('Failed downloading %s with status $d.', self.url, response.status_code)
        return

      # Determine the image file extension.
      ext = imghdr.what(None, h=response.content)
      if ext is None:
        logging.error('Download %s is not an image.', self.url)
        return
      # Set the extension, but prefer 'jpg' in place of 'jpeg'
      self.local_ext = ext if ext != 'jpeg' else 'jpg'

      image_file = io.BytesIO(response.content)

    # Save image file.
    local_filepath = os.path.join(local_dir, self.local_filename())
    with open(local_filepath, 'wb') as f:
        shutil.copyfileobj(image_file, f)
    self.download_successful = True

  def __str__(self):
    return (f'url: {self.url}\n'
            f'passes_filters: {self.passes_filters}\n'
            f'original_filename: {self.original_filename}\n'
            f'local_basename: {self.local_basename}\n'
            f'local_ext: {self.local_ext}\n')


class ImageUrlReplacementPlan:
  """A class which plans and executes image replacement operations on a collection Markdown files."""

  def __init__(self,
               md_filepaths: Sequence[str],
               image_dest_dir: str ,
               markdown_dest_dir: str,
               url_substring_filters: Sequence[str] = None):
    self.md_filepaths = md_filepaths
    self.url_substring_filters = url_substring_filters
    self.image_dest_dir = image_dest_dir
    self.markdown_dest_dir = markdown_dest_dir

    # A map from image URL to its ImageUrlRecord.
    self.image_url_records = {}

    # Collect image URLs by analyzing the Markdown source.
    self._get_image_url_occurrences()
    # Assign unique local filenames for each image.
    self._assign_local_filenames()

  def execute(self):
    """Executes image downloading and reference replacement."""
    # Download all images first.
    for img in self._iterate_image_url_records():
      img.download(self.image_dest_dir)

    # Replace image URLs in all Markdown files where it is possible.
    for filepath, image_url_records in self._get_image_url_records_by_file():
      logging.info('Replacing URLs in file %s...', filepath)
      with open(filepath, 'r') as f:
        md_source = f.read()

      # Sort in reverse order of URL length. Replacing URLs in the order guards against the edge case where a URL is a
      # substring of another.
      image_url_records.sort(reverse=True, key=lambda x: len(x.url))

      # For each URL in this file, check if we can replace it and do so if possible.
      for img in image_url_records:
        file_occurrence = img.get_file_occurrence(filepath)
        if img.download_successful and not file_occurrence.replacement_unsafe():
          replacement = f'({os.path.join(self.markdown_dest_dir, img.local_filename())})'
          md_source = re.sub(build_url_dest_regex(img.url), replacement, md_source)
          file_occurrence.replace_successful = True

      with open(filepath, 'w') as f:
        f.write(md_source)

  def get_execution_plan_string(self) -> str:
    """Returns a detailed description of Markdown editing actions that are planned."""
    print_str = ''

    # Build a summary for URL replacements.
    replacement_plan_str = ''
    for filepath, image_url_records in self._get_image_url_records_by_file():
      file_plan_str = f'- For file: `{filepath}`\n'
      for img in image_url_records:
        file_plan_str += f'\t- For URL: `{img.url}`\n'
        file_occurrence = img.get_file_occurrence(filepath)
        if file_occurrence.replacement_unsafe():
          file_plan_str += f'\t\t- Replacement is unsafe, so will not be attempted.\n'
        else:
          if not img.local_ext:
            file_plan_str += (f'\t\t- Replacement will be attempted with local filename `{img.local_filename()}` '
                              '(file extension is not known yet).\n')
          else:
            file_plan_str += f'\t\t- Replacement will be attempted with local filename `{img.local_filename()}`.\n'
      replacement_plan_str += file_plan_str

    if not replacement_plan_str:
      replacement_plan_str = '- No replacements planned. This may be because there are no matching URLs.\n'
    print_str += '- URL Replacement Plan\n' + textwrap.indent(replacement_plan_str, '\t')

    return print_str

  def get_execution_summary_string(self):
    """Returns a detailed description after execution of any image download failure or Markdown editing failures.

    All edits which can be made successfully are executed and are not included in this execution summary. This summary
    serves as a guide to manually fix cases that cannot be migrated automatically.
    """
    print_str = ''

    # Build a summary for image downloads.
    download_summary_str = ''
    for img in self._iterate_image_url_records():
      if not img.download_successful:
        download_summary_str += f'- Failed to download URL: `{img.url}`\n'
        download_summary_str += f'\t- Occurs in files:\n' + ''.join(
          [f'\t\t- `{file_occurrence.filepath}`\n' for file_occurrence in img.file_occurrences])

    if not download_summary_str:
      download_summary_str = '- All image downloads succeeded!\n'
    print_str += '- Image Download Summary\n' + textwrap.indent(download_summary_str, '\t')

    # Build a summary for URL replacements.
    replacement_summary_str = ''
    for filepath, image_url_records in self._get_image_url_records_by_file():
      has_failed_replacement = False
      file_summary_str = f'- For file: `{filepath}`\n'
      for img in image_url_records:
        file_occurrence = img.get_file_occurrence(filepath)
        if not file_occurrence.replace_successful:
          has_failed_replacement = True
          file_summary_str += f'\t- Failed to replace URL: `{img.url}`\n'
          if not img.download_successful:
            file_summary_str += f'\t\t- Reason: download failed\n'
          elif file_occurrence.replacement_unsafe():
            file_summary_str += ('\t\t- Reason: replacement is unsafe because there are '
                                 f'{file_occurrence.num_extra_textual_occurrences} occurrence(s) '
                                 'of the URL outside an image element\n')
            file_summary_str += f'\t\t- Local image filename: `{img.local_filename()}`\n'
      if has_failed_replacement:
        replacement_summary_str += file_summary_str

    if not replacement_summary_str:
      replacement_summary_str = '- All URLs replaced successfully!\n'
    print_str += '- URL Replacement Summary\n' + textwrap.indent(replacement_summary_str, '\t')

    return print_str

  def _get_image_url_occurrences(self):
    """Analyzes Markdown files and collects image URLs to download and replace."""
    for filepath in self.md_filepaths:
      with open(filepath, 'r') as f:
        md_source = f.read()

      # Counts the number of times each image URL occurs in this file.
      image_url_counts = get_image_url_counts(md_source)
      url_textual_counts = get_textual_counts(md_source, image_url_counts.keys())

      for url, textual_count in url_textual_counts.items():
        # We track whether there are instances of the URL in the file which are outside of an image element. Such
        # "num_extra_textual_occurrences" mean that simply replacing the URL with the local filepath may make an
        # undesired edit.
        num_image_elements = image_url_counts[url]
        num_extra_textual_occurrences = textual_count - num_image_elements

        # Add a record of this image URL if it has not been seen before.
        if url not in self.image_url_records:
          raw_path = urllib.parse.urlparse(url).path
          unquoted_path = urllib.parse.unquote(raw_path)
          original_filename = os.path.basename(unquoted_path)
          self.image_url_records[url] = ImageUrlRecord(
              url=url,
              passes_filters=self._check_passes_filters(url),
              original_filename=original_filename)

        # Record the occurrence of this image URL in this file.
        self.image_url_records[url].file_occurrences.append(
          FileOccurrenceRecord(filepath=filepath,
                               num_image_elements=num_image_elements,
                               num_extra_textual_occurrences=num_extra_textual_occurrences))

  def _assign_local_filenames(self):
    """Assigns unique image filenames to every image."""
    exiting_dir_contents = os.listdir(self.image_dest_dir)
    fn_transformer = LogseqImageFilenameTransformer(exiting_dir_contents)
    for img in self._iterate_image_url_records():
      img.local_basename, img.local_ext = fn_transformer.assign_uniquified_filename(img.original_filename)

  def _check_passes_filters(self, url: str):
    """Checks whether a URL passes any provided filters."""
    if not self.url_substring_filters:
      return True
    for substring_filter in self.url_substring_filters:
      if url.count(substring_filter):
        return True
    return False

  def _iterate_image_url_records(self, filtered: bool = True):
    """Iterates all ImageUrlRecords in a deterministic order."""
    # Sort to get a deterministic order.
    img_records = sorted(list(self.image_url_records.values()), key=lambda x: x.url)
    for img in img_records:
      if filtered and not img.passes_filters:
        continue
      yield img

  def _get_image_url_records_by_file(self, filtered: bool = True):
    """Iterates all (filepath, image_url_records) in a deterministic order."""
    imgs_grouped_by_file = collections.defaultdict(list)
    for img in self._iterate_image_url_records(filtered=filtered):
      for file_occurrence in img.file_occurrences:
        imgs_grouped_by_file[file_occurrence.filepath].append(img)

    # Sort to get a deterministic order.
    for url, imgs in imgs_grouped_by_file.items():
      imgs.sort(key=lambda x: x.url)
    return sorted(list(imgs_grouped_by_file.items()), key=lambda x: x[0])

  def __str__(self):
    print_str = ''
    for filepath, image_url_records in self._get_image_url_records_by_file():
      print_str += f'---\nfilepath: {filepath}\n'

      imgs_str = ''
      for img in image_url_records:
        file_occurrence = img.get_file_occurrence(filepath)
        imgs_str += '\n' + str(img) + str(file_occurrence)
      print_str += textwrap.indent(imgs_str, '\t')

    return print_str

def main(argv: Sequence[str]) -> None:
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')

  # Build the replacement plan.
  replacement_plan = ImageUrlReplacementPlan(
    glob.glob(_INPUT_PATTERN.value),
    _IMAGE_DEST_DIR.value,
    _MARKDOWN_DEST_DIR.value,
    url_substring_filters=_URL_SUBSTRING_FILTERS.value)

  # Create a plan summary and save it.
  plan_summary = replacement_plan.get_execution_plan_string()
  if _PLAN_SUMMARY.value:
    with open(_PLAN_SUMMARY.value, 'w') as f:
      f.write(plan_summary)

  # If this is a dry run, print the plan and exit. Otherwise, execute the plan, then print and save the summary.
  if _DRY_RUN.value:
    print(plan_summary)
  else:
    replacement_plan.execute()
    execution_summary = replacement_plan.get_execution_summary_string()
    print(execution_summary)
    if _EXECUTION_SUMMARY.value:
      with open(_EXECUTION_SUMMARY.value, 'w') as f:
        f.write(execution_summary)


if __name__ == '__main__':
  app.run(main)