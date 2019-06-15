######
#
# api_apply_classifier_single_node.py
#
# Takes the JSON file produced by the detection API and
# classifies all boxes above a certain confidence threshold.
#
######

#%% Constants, imports, environment

import os
import time
import argparse
import json

import tensorflow as tf
import numpy as np
import PIL
import humanfriendly

# Minimum detection confidence for showing a bounding box on the output image
DEFAULT_CONFIDENCE_THRESHOLD = 0.85

# Number of top-scoring classes to show at each bounding box
NUM_ANNOTATED_CLASSES = 3

# Enlargment factor applied to boxes before passing them to the classifier
# Provides more context and can lead to better results
PADDING_FACTOR = 1.6

# List of detection categories, for which we will run the classification
# Currently there are {"1": "animal", "2": "person", "4": "vehicle"}
# Please use strings here
DETECTION_CATEGORY_WHITELIST = ['1']
assert all([isinstance(x, str) for x in DETECTION_CATEGORY_WHITELIST])


#%% Core detection functions

def load_model(checkpoint):
    """
    Load a detection model (i.e., create a graph) from a .pb file
    """

    print('Creating Graph...')
    graph = tf.Graph()
    with graph.as_default():
        od_graph_def = tf.GraphDef()
        with tf.gfile.GFile(checkpoint, 'rb') as fid:
            serialized_graph = fid.read()
            od_graph_def.ParseFromString(serialized_graph)
            tf.import_graph_def(od_graph_def, name='')
    print('...done')

    return graph


def add_classification_categories(json_object, classes_file):
    '''
    Reads the name of classes from the file *classes_file* and adds them to
    the JSON object *json_object*. The function assumes that the first line
    corresponds to output no. 0, i.e. we use 0-based indexing.

    Modifies json_object in-place.

    Args:
    json_object: an object created from a json in the format of the detection API output
    classes_file: the list of classes that correspond to the output elements of the classifier

    Return:
    The modified json_object with classification_categories added. If the field 'classification_categories'
    already exists, then this function is a no-op.
    '''

    if 'classification_categories' not in json_object.keys():

        # Read the name of all classes
        with open(classes_file, 'rt') as fi:
            class_names = fi.read().splitlines()
            # remove empty lines
            class_names = [cn for cn in class_names if cn.strip()]

        # Create field with name *classification_categories*
        json_object['classification_categories'] = dict()
        # Add classes using 0-based indexing
        for idx, name in enumerate(class_names):
            json_object['classification_categories']['%i'%idx] = name
    else:
        print('WARNING: The input json already contains the list of classification categories.')

    return json_object


def classify_boxes(classification_graph, json_with_classes, image_dir, confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                  detection_category_whitelist=DETECTION_CATEGORY_WHITELIST, padding_factor=PADDING_FACTOR,
                  num_annotated_classes=NUM_ANNOTATED_CLASSES):
    '''
    Takes a classification model and applies it to all detected boxes with a detection confidence
    larger than confidence_threshold.

    Args:
        classification_graph: frozen graph model that includes the TF-slim preprocessing. i.e. it will be given a cropped
                              images with values in [0,1]
        json_with_classes:    Object created from the json file that is generated by the detection API. However, the
                              field 'classification_categories' is already added. The script assumes 0-based indexing.
        image_dir:            Base directory of the images. All paths in the JSON are relative to this folder
        confidence_threshold: Only classify boxes with a threshold larger than this
        detection_category_whitelist : Only boxes with this detection category will be classified
        padding_factor:       The function will enlarge the bounding boxes by this factor before passing them to the
                              classifier.
        num_annotated_classes: Number of top-scoring class predictions to store in the json

    Returns the updated json object. Classification results are added as field 'classifications' to all elements images/detections
    assuming a 0-based indexing of the classifier output, i.e. output with index 0 has the class key '0'
    '''

    # Make sure we have the right json object
    assert 'classification_categories' in json_with_classes.keys()
    assert isinstance(detection_category_whitelist, list)
    assert all([isinstance(x, str) for x in detection_category_whitelist])

    with classification_graph.as_default():

        with tf.Session(graph=classification_graph) as sess:

            # Get input and output tensors of classification model
            image_tensor = classification_graph.get_tensor_by_name('input:0')
            predictions_tensor = classification_graph.get_tensor_by_name('output:0')
            predictions_tensor = tf.squeeze(predictions_tensor, [0])

            # For each image
            nImages = len(json_with_classes['images'])
            for iImage in range(0,nImages):

                image_description = json_with_classes['images'][iImage]

                # Read image
                try:
                    image_path = image_description['file']
                    if image_dir:
                        image_path = os.path.join(image_dir, image_path)
                    image_data = np.array(PIL.Image.open(image_path).convert("RGB"))
                    # Scale pixel values to [0,1]
                    image_data = image_data / 255
                    image_height, image_width, _ = image_data.shape
                except KeyboardInterrupt as e:
                    raise e
                except:
                    print('Couldn\' load image {}'.format(image_path))
                    continue

                # For each box
                nDetections = len(image_description['detections'])
                for iBox in range(nDetections):

                    cur_detection = image_description['detections'][iBox]

                    # Skip detections with low confidence
                    if cur_detection['conf'] < confidence_threshold:
                        continue

                    # Skip if detection category is not in whitelist
                    if not cur_detection['category'] in detection_category_whitelist:
                        continue

                    # Skip if already classified
                    if 'classifications' in cur_detection.keys() and len(cur_detection['classifications']) > 0:
                        continue

                    # Get current box in relative coordinates and format [x_min, y_min, width_of_box, height_of_box]
                    box_orig = cur_detection['bbox']
                    # Convert to [ymin, xmin, ymax, xmax] and
                    # store it as 1x4 numpy array so we can re-use the generic multi-box padding code
                    box_coords = np.array([[box_orig[1],
                                            box_orig[0],
                                            box_orig[1]+box_orig[3],
                                            box_orig[0]+box_orig[2]
                                          ]])
                    # Convert normalized coordinates to pixel coordinates
                    box_coords_abs = (box_coords * np.tile([image_height, image_width], (1,2)))
                    # Pad the detected animal to a square box and additionally by PADDING_FACTOR, the result will be in crop_boxes
                    # However, we need to make sure that it box coordinates are still within the image
                    bbox_sizes = np.vstack([box_coords_abs[:,2] - box_coords_abs[:,0], box_coords_abs[:,3] - box_coords_abs[:,1]]).T
                    offsets = (padding_factor * np.max(bbox_sizes, axis=1, keepdims=True) - bbox_sizes) / 2
                    crop_boxes = box_coords_abs + np.hstack([-offsets,offsets])
                    crop_boxes = np.maximum(0,crop_boxes).astype(int)
                    # Get the first (and only) row as our bbox to classify
                    crop_box = crop_boxes[0]

                    # Get the image data for that box
                    cropped_img = image_data[crop_box[0]:crop_box[2], crop_box[1]:crop_box[3]]
                    # Run inference
                    predictions = sess.run(predictions_tensor, feed_dict={image_tensor: cropped_img})

                    # Add an empty list to the json for our predictions
                    cur_detection['classifications'] = list()
                    # Add the *num_annotated_classes* top scoring classes
                    for class_idx in np.argsort(-predictions)[:num_annotated_classes]:
                        cur_detection['classifications'].append(['%i'%class_idx, predictions[class_idx].item()])

                # ...for each box

            # ...for each image

        # ...with tf.Session

    # with classification_graph

    return json_with_classes


def load_and_run_classifier(classifier_file, classes_file, image_dir, detector_json_file, output_json_file,
                          confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD, padding_factor=PADDING_FACTOR,
                          num_annotated_classes=NUM_ANNOTATED_CLASSES, detection_category_whitelist=DETECTION_CATEGORY_WHITELIST,
                          detection_graph=None, classification_graph=None):

    # Load classification model
    if classification_graph is None:
        classification_graph = load_model(classifier_file)

    # Load detector json
    with open(detector_json_file, 'rt') as fi:
        detector_json = json.load(fi)

    # Add classes to detector_json
    updated_json = add_classification_categories(detector_json, classes_file)

    # Run classifier on all images, changes will be writting directly to the json
    startTime = time.time()
    updated_json = classify_boxes(classification_graph, updated_json, image_dir, confidence_threshold, detection_category_whitelist,
                                  padding_factor, num_annotated_classes)
    elapsed = time.time() - startTime
    print("Done running detector and classifier in {}".format(humanfriendly.format_timespan(elapsed)))

    # Write output json
    with open(output_json_file, 'wt') as fi:
        json.dump(updated_json, fi, indent=4)

    return detection_graph, classification_graph


#%% Command-line driver

def main():
    parser = argparse.ArgumentParser(description='Applies a classifier to all detected boxes of the detection API output (JSON format).')
    parser.add_argument('classifier_file', type=str, help='Frozen graph for classification including pre-processing. The graphs ' + \
                        ' will receive an image with values in [0,1], so double check that you use the correct model. The script ' + \
                        ' `export_inference_graph_serengeti.sh` shows how to create such a model',
                       metavar='PATH_TO_CLASSIFIER_W_PREPROCESSING')
    parser.add_argument('classes_file', action='store', type=str, help='File with the class names. Each line should contain ' + \
                        ' one name and the first line should correspond to the first output, the second line to the second model output, etc.')
    parser.add_argument('detector_json_file', type=str, help='JSON file that was produced by the detection API.')
    parser.add_argument('output_json_file', type=str, help='Path to output file, will be in JSON format.')
    parser.add_argument('--image_dir', action='store', type=str, default='', help='Base directory of the images. Default: ""')
    parser.add_argument('--threshold', action='store', type=float, default=DEFAULT_CONFIDENCE_THRESHOLD,
                        help="Confidence threshold, don't render boxes below this confidence. Default: %.2f"%DEFAULT_CONFIDENCE_THRESHOLD)
    parser.add_argument('--padding_factor', action='store', type=float, default=PADDING_FACTOR,
                        help="Enlargement factor for bounding boxes before they are passed to the classifier. Default: %.2f"%PADDING_FACTOR)
    parser.add_argument('--num_annotated_classes', action='store', type=int, default=NUM_ANNOTATED_CLASSES,
                        help='Number of top-scoring classes to add to the output for each bounding box, default: %d'%NUM_ANNOTATED_CLASSES)
    parser.add_argument('--detection_category_whitelist', type=str, nargs='+', default=DETECTION_CATEGORY_WHITELIST,
                        help='We will run the detector on all detections with these detection categories, default: ' + ' '.join(DETECTION_CATEGORY_WHITELIST))
    args = parser.parse_args()


    load_and_run_classifier(classifier_file=args.classifier_file, classes_file=args.classes_file, image_dir=args.image_dir,
                          detector_json_file=args.detector_json_file, output_json_file=args.output_json_file,
                          confidence_threshold=args.threshold, padding_factor=args.padding_factor,
                          num_annotated_classes=args.num_annotated_classes, detection_category_whitelist=args.detection_category_whitelist)



if __name__ == '__main__':

    main()
