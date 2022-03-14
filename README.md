# markdown_image_downloader
A tool to replace remote image URLs with locally downloaded images in Markdown files. Developed for Migrating from Roam to Logseq.

## Usage

This script analyses a set of Markdown files to find remote image URLs, assigns unique filenames for each image, downloads the image, and edits the Markdown source to reference the local copy of the image.

The result of executing the script is that:
- Images are downloaded if possible (failures are recorded for manual cleanup).
- The local filename of the image will be a uniquified version of the original image filename.
- If the URL doesn't have a file extension, the image data is inspected to determine the correct extension.
- Markdown references to the image URL are replaced by a reference to a local file where feasible (infeasible cases are recorded, see below).
- Image references are kept consistent. E.g. if file `a.md` and `b.md` both reference the same image URL, then they will both reference the same local image file after running this script.

See the docstring in `markdown_image_downloader.py` for more detailed usage instructions. Please `pip install -r requirements.txt` before using.

### Infeasible edits

As mentioned, there are some infeasible cases where an edit to the Markdown cannot be safely made. For example:

```md
Here is an image:

![](https://example.com/a.jpg)

The markdown for this image is `![](https://example.com/a.jpg)`.
```

In this example, the script finds the image element, which is a candidate for replacement. But it also finds the textual reference to the URL outside of an image element. In this scenario, textually replacing the image URL with its local filepath will change the image element (as desired) but also the later reference (which may not be desired). This these cases, `markdown_image_downloader.py` downloads the image file, but does not make the Markdown edit. Instead, it records such cases in the output execution summary, which can serve as a guide for making the correct edit manually.
